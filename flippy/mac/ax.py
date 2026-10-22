"""Background desktop tasks on macOS: one app's window, worked through the Accessibility API.

/act no longer drives the real pointer and keyboard. It works on a target app (the one in front when the task
started, or one it switched to with use_app), whether or not that app is in front:

  see:  that app's window, captured on its own (CGWindowListCreateImage with its window id, so other windows
        on top don't matter), plus a numbered list of its controls from the Accessibility API
  act:  press / set a control's text / focus it / pick a menu item, all through the Accessibility API, and
        typing and shortcuts sent to that app's process (CGEventPostToPid), not to whatever is in front

Your pointer never moves and the app you're in keeps the keyboard. Clicking, scrolling and dragging by
coordinates come later (pointer-free events to the window, then borrowing the mouse while you're idle).

AX calls are IPC to the target app, so they run on the task's worker thread, not the Cocoa run loop.
"""
import base64
import io
import os
import subprocess
import time

import AppKit
import ApplicationServices as AX
import Quartz

from ..actions import ActionError, RetryableActionError
from . import hotkeys, keymap

MAX_ELEMENTS = 150      # controls listed per look
MAX_DEPTH = 30
MAX_VISITED = 4000      # AX nodes walked per look, so a huge table can't stall the task
VALUE_CHARS = 80        # a control's current value, as shown to Claude
SEND_LONG_EDGE = 1600
LAUNCH_WAIT_S = 10.0

# controls worth listing: things you can press, type into, pick, or read as a label
ROLES = {
    "AXButton": "button", "AXMenuButton": "menu button", "AXPopUpButton": "pop-up", "AXCheckBox": "checkbox",
    "AXRadioButton": "radio", "AXTextField": "text field", "AXTextArea": "text area", "AXSearchField": "search field",
    "AXComboBox": "combo box", "AXSlider": "slider", "AXIncrementor": "stepper", "AXLink": "link", "AXTab": "tab",
    "AXRadioGroup": "tab group", "AXDisclosureTriangle": "disclosure", "AXRow": "row", "AXCell": "cell",
    "AXOutlineRow": "row", "AXMenuItem": "menu item", "AXColorWell": "color well", "AXDateField": "date field",
    "AXSegmentedControl": "segmented", "AXSwitch": "switch", "AXStaticText": "text", "AXHeading": "heading",
    "AXImage": "image", "AXWebArea": "web area",
}
PRESS_ACTIONS = ("AXPress", "AXConfirm", "AXPick", "AXOpen")
ERR_GONE = (-25202, -25204)   # kAXErrorInvalidUIElement, kAXErrorCannotComplete (app busy or element gone)


def trusted():
    return bool(AX.AXIsProcessTrusted())


def ask_trust():
    """Ask macOS for the Accessibility permission: its prompt, and Flippy added (switched off) to the list in
    System Settings, so nobody has to find the app and drag it in. Shown once per app build; True if already allowed."""
    return bool(AX.AXIsProcessTrustedWithOptions({AX.kAXTrustedCheckOptionPrompt: True}))


def _attr(el, name):
    err, value = AX.AXUIElementCopyAttributeValue(el, name, None)
    return value if err == 0 else None


def _actions(el):
    err, names = AX.AXUIElementCopyActionNames(el, None)
    return list(names or []) if err == 0 else []


def _settable(el, name="AXValue"):
    err, ok = AX.AXUIElementIsAttributeSettable(el, name, None)
    return err == 0 and bool(ok)


def _point(value, kind):
    """AXValueRef (position or size) -> (a, b)."""
    if value is None:
        return None
    ok, out = AX.AXValueGetValue(value, kind, None)
    if not ok:
        return None
    return (out.x, out.y) if kind == AX.kAXValueCGPointType else (out.width, out.height)


def _frame(el):
    pos = _point(_attr(el, "AXPosition"), AX.kAXValueCGPointType)
    size = _point(_attr(el, "AXSize"), AX.kAXValueCGSizeType)
    return (pos[0], pos[1], size[0], size[1]) if pos and size else None


def _short(value):
    if value is None:
        return ""
    text = str(value).replace("\n", " ").strip()
    return text if len(text) <= VALUE_CHARS else text[:VALUE_CHARS - 1] + "…"


# ---- which app

def _running():
    """Every running app. Read from NSWorkspace's list: NSRunningApplication's lookups by pid or bundle id, called
    from the task's worker thread, returned nothing for an app launched moments earlier (a task stopped with
    "Spotify quit" while Spotify ran under exactly that pid), while this list had it."""
    return [a for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications() if not a.isTerminated()]


def running_app(pid):
    return next((a for a in _running() if a.processIdentifier() == pid), None)


def _relaunched(bundle, old_pid):
    """The same app under a new process (it restarted, or swapped processes), or None if it's really gone."""
    if not bundle:
        return None
    apps = [a for a in _running() if str(a.bundleIdentifier() or "") == bundle
            and a.activationPolicy() == AppKit.NSApplicationActivationPolicyRegular]
    if any(a.processIdentifier() == old_pid for a in apps):
        return old_pid
    return apps[0].processIdentifier() if apps else None


FINDER = "com.apple.finder"
_last_front = None   # (bundle id, name, pid) of the last app in front that wasn't Flippy
_observer = None


def _is_flippy(app):
    """Flippy's own process, its launcher (Flippy.app's executable), or any Flippy build."""
    return app.processIdentifier() in (os.getpid(), os.getppid()) or \
        str(app.bundleIdentifier() or "").startswith("dev.flippy.")


def _remember(app):
    global _last_front
    if app is not None and not _is_flippy(app) and \
            app.activationPolicy() == AppKit.NSApplicationActivationPolicyRegular:
        _last_front = str(app.bundleIdentifier() or ""), str(app.localizedName()), app.processIdentifier()


def watch_front():
    """Remember the last app in front that isn't Flippy. Settings, setup and the tour bring Flippy forward, and a
    task started right after should still go to the app you were in. Main thread, once."""
    global _observer
    if _observer is not None:
        return
    ws = AppKit.NSWorkspace.sharedWorkspace()
    _remember(ws.frontmostApplication())
    _observer = ws.notificationCenter().addObserverForName_object_queue_usingBlock_(
        AppKit.NSWorkspaceDidActivateApplicationNotification, None, None,
        lambda note: _remember(note.userInfo().get(AppKit.NSWorkspaceApplicationKey)))


def give_back_front():
    """A Flippy window closed: bring back the app that was in front before it, instead of leaving Flippy (with no
    window) as the active app."""
    app = running_app(_last_front[2]) if _last_front else None
    if app is not None:
        app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)


def front_app():
    """(bundle id, name, pid) of the app the task starts in: the one in front, or the last one that was when Flippy
    is (its settings or setup were just open), or Finder, from where use_app can open any app."""
    app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is not None and not _is_flippy(app):
        return str(app.bundleIdentifier() or ""), str(app.localizedName()), app.processIdentifier()
    if _last_front and running_app(_last_front[2]) is not None:
        return _last_front
    finder = next((a for a in _running() if str(a.bundleIdentifier() or "") == FINDER), None)
    if finder is None:
        raise ActionError("Click the app you want Flippy to work in, then ask again.")
    return FINDER, str(finder.localizedName()), finder.processIdentifier()


TEXT_ROLES = ("AXTextArea", "AXTextField")
EDITOR_MIN = (200, 80)  # a multi-line text area smaller than this is a stand-in for the editor around it


QUICK_S = 0.25  # how long a lookup for answer placement waits on a busy app


def _quick(el):
    """el, with lookups on it giving up after QUICK_S: placement never waits on a busy app."""
    if el is not None:
        AX.AXUIElementSetMessagingTimeout(el, QUICK_S)
    return el


def _working_app():
    """The AX element of the app you were in (Flippy's question box may be in front by now), or None."""
    app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    pid = app.processIdentifier() if app is not None and not _is_flippy(app) else (_last_front or (0, 0, 0))[2]
    return _quick(AX.AXUIElementCreateApplication(pid)) if pid else None


def prime_focused_area():
    """Electron apps (VS Code, Slack...) only build their accessibility tree when asked to: ask now, as the question
    box opens, so it's there by the time the question is sent. Other apps ignore it."""
    if trusted() and (root := _working_app()) is not None:
        AX.AXUIElementSetAttributeValue(root, "AXManualAccessibility", True)


def focused():
    """(the focused element, its app's focused window) of the app you're working in: two quick lookups, cheap enough
    for the main thread as the box opens. Their frames are read later, off it (work_area)."""
    root = _working_app() if trusted() else None
    if root is None:
        return None, None
    return _quick(_attr(root, "AXFocusedUIElement")), _quick(_attr(root, "AXFocusedWindow"))


def window_frame(window):
    """(x, y, w, h) in screen points of a window element, or None. Apps that report no text area (browsers) still
    report their window."""
    frame = _frame(window) if window is not None else None
    return frame if frame and frame[2] > 0 and frame[3] > 0 else None


def text_area(el):
    """(x, y, w, h) in screen points of the text you're working in (an editor, a terminal, a document) when el is
    the focused element, or None. Only the part inside its window: an editor's text area can be the whole
    document."""
    role = _attr(el, "AXRole") if el is not None else None
    if role not in TEXT_ROLES:
        return None
    frame = _frame(el)
    if role == "AXTextArea":
        for _ in range(6):  # VS Code focuses a hidden one-line field at the cursor: use the editor around it
            if not frame or frame[2] >= EDITOR_MIN[0] and frame[3] >= EDITOR_MIN[1]:
                break
            el = _quick(_attr(el, "AXParent"))
            frame = _frame(el) if el is not None else None
    window = _quick(_attr(el, "AXWindow")) if el is not None else None
    bounds = _frame(window) if window is not None else None
    if not frame:
        return None
    if bounds:
        x0, y0 = max(frame[0], bounds[0]), max(frame[1], bounds[1])
        x1 = min(frame[0] + frame[2], bounds[0] + bounds[2])
        y1 = min(frame[1] + frame[3], bounds[1] + bounds[3])
        frame = (x0, y0, x1 - x0, y1 - y0)
    return frame if frame[2] > 0 and frame[3] > 0 else None


def find_app(name):
    """A running app by name (case-insensitive), or None."""
    want = name.strip().lower()
    for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
        if app.activationPolicy() == AppKit.NSApplicationActivationPolicyRegular and \
                str(app.localizedName() or "").lower() == want and app.processIdentifier() != os.getpid():
            return str(app.bundleIdentifier() or ""), str(app.localizedName()), app.processIdentifier()
    return None


def _bundle_for(name):
    """The bundle id of the app `open -a name` opens ("Chrome" -> com.google.Chrome), or None."""
    path = AppKit.NSWorkspace.sharedWorkspace().fullPathForApplication_(name.strip()) or _app_named_like(name)
    bundle = AppKit.NSBundle.bundleWithPath_(path) if path else None
    return str(bundle.bundleIdentifier()) if bundle is not None and bundle.bundleIdentifier() else None


APP_DIRS = ("/Applications", "/System/Applications", "/System/Applications/Utilities", "/Applications/Utilities",
            os.path.expanduser("~/Applications"))


def _app_named_like(name):
    """An installed app whose name contains every word of `name` ("chrome" -> Google Chrome.app), the shortest
    such name, or None."""
    words = name.lower().replace(".app", "").split()
    if not words:
        return None
    hits = []
    for d in APP_DIRS:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        hits += [os.path.join(d, n) for n in names if n.endswith(".app") and all(w in n.lower() for w in words)]
    return min(hits, key=lambda p: len(os.path.basename(p)), default=None)


def _running_bundle(bundle):
    """A running app with that bundle id, as (bundle, name, pid), or None."""
    for app in _running():
        if str(app.bundleIdentifier() or "") == bundle and not _is_flippy(app) and \
                app.activationPolicy() == AppKit.NSApplicationActivationPolicyRegular:
            return bundle, str(app.localizedName()), app.processIdentifier()
    return None


def open_app(name, cancel):
    """Find the app by name, or open it without bringing it to the front (`open -g`). (bundle, name, pid).
    A loose name ("Chrome", "vscode") is matched by the bundle id it resolves to, not only the exact app name."""
    found = find_app(name)
    if found:
        return found
    bundle = _bundle_for(name)
    found = _running_bundle(bundle) if bundle else None
    if found:
        return found
    r = subprocess.run(["/usr/bin/open", "-g", *(["-b", bundle] if bundle else ["-a", name])],
                       capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        raise RetryableActionError(f"There's no app called {name!r} on this Mac. Check the name and try again.")
    deadline = time.monotonic() + LAUNCH_WAIT_S
    while time.monotonic() < deadline and not cancel.is_set():
        found = find_app(name) or (_running_bundle(bundle) if bundle else None)
        if found and _window(found[2]) is not None:
            return found
        time.sleep(0.25)
    if found:
        return found
    raise RetryableActionError(f"{name} didn't open in time. Try use_app again.")


# ---- its window

def _window(pid):
    """The app's focused (or main, or first) window as an AX element, or None."""
    app = AX.AXUIElementCreateApplication(pid)
    for name in ("AXFocusedWindow", "AXMainWindow"):
        win = _attr(app, name)
        if win is not None:
            return win
    wins = _attr(app, "AXWindows") or []
    return wins[0] if wins else None


def _window_id(pid, frame, title):
    """The CG window number matching that AX window (same owner, same bounds; title breaks ties)."""
    info = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID) or []
    best = None
    for win in info:
        if win.get("kCGWindowOwnerPID") != pid or win.get("kCGWindowLayer", 0) != 0:
            continue
        b = win.get("kCGWindowBounds") or {}
        if frame and all(abs(a - c) < 2 for a, c in zip((b.get("X"), b.get("Y"), b.get("Width"), b.get("Height")), frame)):
            if best is None or (title and win.get("kCGWindowName") == title):
                best = int(win["kCGWindowNumber"])
    return best


def _capture(window_id):
    """That window alone, as a PIL image (other windows on top don't show), or None."""
    from PIL import Image
    img = Quartz.CGWindowListCreateImage(Quartz.CGRectNull, Quartz.kCGWindowListOptionIncludingWindow, window_id,
                                         Quartz.kCGWindowImageBoundsIgnoreFraming | Quartz.kCGWindowImageNominalResolution)
    if img is None or Quartz.CGImageGetWidth(img) <= 1:
        return None
    w, h = Quartz.CGImageGetWidth(img), Quartz.CGImageGetHeight(img)
    data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(img))
    return Image.frombuffer("RGBA", (w, h), bytes(data), "raw", "BGRA", Quartz.CGImageGetBytesPerRow(img), 1)


def look(bundle, name, pid):
    """What Claude sees: (jpeg b64 or None, image size, target, elements, description).
    elements: {id: (AX element, role, label, actions)}; ids are only good until the next look.
    No window, or one that's minimized or can't be captured, isn't a dead end: its controls (if any) and the
    app's menus are still listed, and menus and media keys work without a window."""
    # The app by its bundle id first (a process id can go stale), the process id only when there's no bundle id
    current = (_relaunched(bundle, pid) if bundle else None) or (pid if running_app(pid) is not None else None)
    if current is None:
        running = [(str(a.bundleIdentifier()), a.processIdentifier(), a.isTerminated())
                   for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications()
                   if str(a.localizedName() or "") == name]
        print(f"flippy: act: {name} looks gone: had pid {pid} bundle {bundle!r}; running with that name: {running}",
              flush=True)
        raise ActionError(f"{name} quit. Start a new /act request.")
    pid = current
    win = _window(pid)
    minimized = bool(win is not None and _attr(win, "AXMinimized"))
    frame = _frame(win) if win is not None else None
    title = _short(_attr(win, "AXTitle")) if win is not None else ""
    wid = _window_id(pid, frame, title) if frame and not minimized else None
    image = _capture(wid) if wid else None
    if image is not None:
        k = min(1.0, SEND_LONG_EDGE / max(image.size))
        if k < 1:
            image = image.resize((round(image.width * k), round(image.height * k)))
        sx, sy = image.width / frame[2], image.height / frame[3]   # screen points -> image pixels
    else:
        sx = sy = 1.0
        frame = frame or (0, 0, 0, 0)

    elements, lines, visited = {}, [], 0

    def visit(el, depth):
        nonlocal visited
        visited += 1
        if depth > MAX_DEPTH or visited > MAX_VISITED or len(elements) >= MAX_ELEMENTS:
            return
        role = _attr(el, "AXRole")
        kind = ROLES.get(role)
        if kind:
            label = _short(_attr(el, "AXTitle") or _attr(el, "AXDescription") or _attr(el, "AXHelp")
                           or _attr(el, "AXPlaceholderValue"))
            value = _short(_attr(el, "AXValue"))
            if role == "AXStaticText" and not label:
                label, value = value, ""
            if value == label:  # (a text whose label is a description, like Calculator's display, keeps its value)
                value = ""
            acts = [a for a in _actions(el) if a in PRESS_ACTIONS]
            can_set = _settable(el)
            box = _frame(el)
            if (acts or can_set or label or value) and box and box[2] > 0 and box[3] > 0:
                n = len(elements) + 1
                ops = (["press"] if acts else []) + (["set_text", "focus"] if can_set else [])
                where = f"({round((box[0] - frame[0]) * sx)},{round((box[1] - frame[1]) * sy)} " \
                        f"{round(box[2] * sx)}x{round(box[3] * sy)})"
                lines.append(f"[{n}] {kind} {label!r}" + (f" = {value!r}" if value else "") + f" {where}"
                             + (f" [{', '.join(ops)}]" if ops else ""))
                elements[n] = (el, role, label or kind, acts, can_set)
        for child in _attr(el, "AXChildren") or []:
            visit(child, depth + 1)
    if win is not None:
        visit(win, 0)

    if image is not None:
        state = (f"window {title!r}, screenshot {image.width}x{image.height} px "
                 f"(control positions are in these pixels)")
    elif win is None:
        state = "no window open. Use its menus (or media for playback); a menu like File > New or Window may open one"
    elif minimized:
        state = f"window {title!r} is minimized: no screenshot, but its controls below can still be used"
    else:
        state = f"window {title!r} can't be captured (another desktop?): no screenshot, controls listed below"
    jpeg = None
    if image is not None:
        buf = io.BytesIO()
        image.convert("RGB").save(buf, format="JPEG", quality=80)
        jpeg = base64.b64encode(buf.getvalue()).decode()
    text = (f"App: {name} — {state}.\n"
            f"Menus (items marked (off) are greyed out right now):\n{_menus(pid)}\n"
            f"Controls ({len(elements)}{'+' if len(elements) >= MAX_ELEMENTS else ''}):\n"
            + ("\n".join(lines) if lines else "(none)"))
    target = (bundle, pid, wid, frame, 0)
    return jpeg, image.size if image is not None else (0, 0), target, elements, text


MENU_ITEMS = 25     # items listed per menu


def _menus(pid):
    """The app's menus and their items, one line per menu: "Playback: Play, Next, Previous (off), ..."."""
    out = []
    bar = _attr(AX.AXUIElementCreateApplication(pid), "AXMenuBar")
    for top in (_attr(bar, "AXChildren") or [])[1:]:  # skip the Apple menu
        title = _short(_attr(top, "AXTitle"))
        items = []
        for menu_el in _attr(top, "AXChildren") or []:
            for it in _attr(menu_el, "AXChildren") or []:
                t = str(_attr(it, "AXTitle") or "").strip()
                if t:
                    items.append(t + (" (off)" if _attr(it, "AXEnabled") is False else "")
                                 + (" >" if _attr(it, "AXChildren") else ""))
        if title:
            out.append(f"  {title}: " + ", ".join(items[:MENU_ITEMS]) + (", ..." if len(items) > MENU_ITEMS else ""))
    return "\n".join(out) or "  (none)"


def _can_post():
    if not Quartz.CGPreflightPostEventAccess():
        raise ActionError("Allow Flippy in macOS Accessibility settings, then restart it.")


NX_KEYS = {"play_pause": 16, "next": 17, "previous": 18}   # NX_KEYTYPE_PLAY / NEXT / PREVIOUS


def media(action):
    """The keyboard's media keys: they go to whatever is playing (Spotify, Music, a video in a browser), with no
    window, pointer or focus involved."""
    _can_post()
    key = NX_KEYS[action]
    for flags in (0xA00, 0xB00):  # key down, key up
        ev = AppKit.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            AppKit.NSEventTypeSystemDefined, (0, 0), flags, 0, 0, None, 8, (key << 16) | flags, -1)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev.CGEvent())


# ---- acting

def _check(cancel):
    if cancel.is_set():
        raise ActionError("Task canceled.")


def _gone(err):
    if err in ERR_GONE:
        raise RetryableActionError("That control is gone or the app is busy. Look again, then try.")


def press(el, acts):
    for action in acts or PRESS_ACTIONS[:1]:
        err = AX.AXUIElementPerformAction(el, action)
        if err == 0:
            return
        _gone(err)
    raise RetryableActionError("That control can't be pressed. Look again and pick another, or use the menus.")


def set_text(el, text):
    if not _settable(el):
        raise RetryableActionError("That control's text can't be set directly. focus it and use type instead.")
    err = AX.AXUIElementSetAttributeValue(el, "AXValue", text)
    _gone(err)
    if err != 0:
        raise RetryableActionError("The app refused that text. focus the control and use type instead.")


def focus(el, pid):
    win = _window(pid)
    if win is not None:
        AX.AXUIElementPerformAction(win, "AXRaise")  # front within its app; the app itself stays where it is
    err = AX.AXUIElementSetAttributeValue(el, "AXFocused", True)
    _gone(err)
    if err != 0:
        raise RetryableActionError("That control can't take focus. Pick a text field or text area.")


def menu(pid, path):
    """Pick a menu item by its titles, e.g. ["File", "New Note"], without opening the menu on screen."""
    bar = _attr(AX.AXUIElementCreateApplication(pid), "AXMenuBar")
    node = bar
    for i, title in enumerate(path):
        items = []
        for child in _attr(node, "AXChildren") or []:
            # a menu bar item / submenu item holds its AXMenu as its only child
            if _attr(child, "AXRole") == "AXMenu":
                items.extend(_attr(child, "AXChildren") or [])
            else:
                items.append(child)
        match = next((it for it in items if str(_attr(it, "AXTitle") or "").strip().lower() == title.strip().lower()),
                     None)
        if match is None:
            names = [str(_attr(it, "AXTitle")) for it in items if _attr(it, "AXTitle")]
            raise RetryableActionError(f"No menu item {title!r} there. Choices: {', '.join(names[:40])}")
        node = match
    if _attr(node, "AXEnabled") is False:
        raise RetryableActionError(f"{' > '.join(path)} is greyed out right now.")
    err = AX.AXUIElementPerformAction(node, "AXPress")
    _gone(err)
    if err != 0:
        raise RetryableActionError(f"Couldn't pick {' > '.join(path)}.")


def _post(pid, code, down, flags=0, text=None):
    ev = Quartz.CGEventCreateKeyboardEvent(None, code, down)
    Quartz.CGEventSetFlags(ev, flags)
    if text is not None:
        Quartz.CGEventKeyboardSetUnicodeString(ev, len(text.encode("utf-16-le")) // 2, text)
    Quartz.CGEventPostToPid(pid, ev)


def type_text(pid, text, cancel):
    """Typing that goes to that app's focused control, wherever you are. Inserts at its cursor through the
    Accessibility API when the control allows it (works in the background); otherwise key events to the app."""
    focused = _attr(AX.AXUIElementCreateApplication(pid), "AXFocusedUIElement")
    if focused is not None and _settable(focused, "AXSelectedText"):
        err = AX.AXUIElementSetAttributeValue(focused, "AXSelectedText", text)
        if err == 0:
            return
        _gone(err)
    _can_post()
    for ch in text:
        _check(cancel)
        code, shifted = keymap.type_key(ch, hotkeys.KEYS)  # the text itself rides on the event
        flags = Quartz.kCGEventFlagMaskShift if shifted else 0
        _post(pid, code, True, flags, ch)
        _post(pid, code, False, flags, ch)
        time.sleep(0.012)


# macOS virtual key codes for keys hotkeys.KEYS (letters, digits, space...) doesn't have
NAV_KEYS = {"return": 36, "tab": 48, "space": 49, "delete": 51, "escape": 53, "left": 123, "right": 124,
            "down": 125, "up": 126, "forwarddelete": 117, "home": 115, "end": 119, "pageup": 116, "pagedown": 121}


def key(pid, combo, flags_by_mod):
    _can_post()
    parts = [p.strip().lower() for p in combo.split("+")]
    code = NAV_KEYS.get(parts[-1], hotkeys.KEYS.get(parts[-1]))
    if code is None:
        raise RetryableActionError(f"Unknown key {parts[-1]!r}.")
    flags = 0
    for m in parts[:-1]:
        flags |= flags_by_mod[m]
    _post(pid, code, True, flags)
    _post(pid, code, False, flags)


# ---- by position, for controls the Accessibility API doesn't list (Spotify's Play button, canvases...)
# Coordinates arrive in screen points (the controller converts from the look's pixels). The events go to the app's
# process with its window id on them (CGEventPostToPid), so your pointer doesn't move and the app stays where it
# is. Many apps accept these in the background; when one doesn't, the next look shows nothing happened.

PRESSABLE = {"AXButton", "AXLink", "AXCheckBox", "AXRadioButton", "AXMenuButton", "AXPopUpButton", "AXTab",
             "AXDisclosureTriangle", "AXSwitch", "AXMenuItem"}


def _mouse(pid, wid, kind, x, y, clicks=1):
    ev = Quartz.CGEventCreateMouseEvent(None, kind, (x, y), Quartz.kCGMouseButtonLeft)
    Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventClickState, clicks)
    if wid:  # the window it's for: the two public fields, and 51, which the window server reads too
        for field in (Quartz.kCGMouseEventWindowUnderMousePointer,
                      Quartz.kCGMouseEventWindowUnderMousePointerThatCanHandleThisEvent, 51):
            Quartz.CGEventSetIntegerValueField(ev, field, wid)
    Quartz.CGEventPostToPid(pid, ev)


def _press_at(pid, x, y):
    """If a real control sits at that point, press it through the Accessibility API. True if it did."""
    err, el = AX.AXUIElementCopyElementAtPosition(AX.AXUIElementCreateApplication(pid), x, y, None)
    if err != 0 or el is None or _attr(el, "AXRole") not in PRESSABLE:
        return False
    return AX.AXUIElementPerformAction(el, "AXPress") == 0


def click(pid, wid, x, y, count, cancel):
    if count == 1 and _press_at(pid, x, y):
        return "pressed"
    _can_post()
    for n in range(1, count + 1):
        _check(cancel)
        _mouse(pid, wid, Quartz.kCGEventLeftMouseDown, x, y, n)
        time.sleep(0.03)
        _mouse(pid, wid, Quartz.kCGEventLeftMouseUp, x, y, n)
        time.sleep(0.06)
    return "clicked"


def scroll(pid, wid, x, y, direction, lines):
    _can_post()
    dy = lines if direction == "up" else -lines if direction == "down" else 0
    dx = lines if direction == "left" else -lines if direction == "right" else 0
    ev = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 2, dy, dx)
    Quartz.CGEventSetLocation(ev, (x, y))
    if wid:
        Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventWindowUnderMousePointer, wid)
    Quartz.CGEventPostToPid(pid, ev)


def drag(pid, wid, x, y, to_x, to_y, cancel, steps=24, seconds=0.6):
    _can_post()
    _mouse(pid, wid, Quartz.kCGEventLeftMouseDown, x, y)
    try:
        for i in range(1, steps + 1):
            _check(cancel)
            t = i / steps
            _mouse(pid, wid, Quartz.kCGEventLeftMouseDragged, x + (to_x - x) * t, y + (to_y - y) * t)
            time.sleep(seconds / steps)
    finally:
        _mouse(pid, wid, Quartz.kCGEventLeftMouseUp, to_x, to_y)  # always let go, even when stopped midway
