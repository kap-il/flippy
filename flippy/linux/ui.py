"""Linux (COSMIC/Wayland) windows: gtk4-layer-shell overlay, GTK question box, portal screenshots, panel icon.

COSMIC quirk: unmapping a layer surface (hide or destroy) makes the compositor
drop our whole Wayland connection, and resizing a mapped one renders garbled.
So both surfaces are mapped once and never unmapped or resized:
  - the overlay is a permanent full-screen, click-through surface; the answer
    card and pointer are drawn inside it and toggled at the widget level;
  - the input box is a regular window instead (see InputBox), and so is the
    invisible window that catches Esc while drawing (see KeyCatcher);
  - help mode's, tips' and updates' cards are drawn in the overlay too
    (flippy/linux/notice.py), not in a surface of their own.

What happens in other apps (the active window, input idle time, where the mouse
is) comes from a second, private Wayland connection: flippy/linux/wl.py.

Run via bin/flippy-daemon (sets LD_PRELOAD for gtk4-layer-shell).
"""
import math
import sys
import threading
import time

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402
from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402

from .. import loop, settings, themes  # noqa: E402
from ..history import PROMPTS  # noqa: E402
from ..overlay import OverlayBase  # noqa: E402
from ..shell.window import AppHost  # noqa: E402
from . import act, preload, sensors, wl  # noqa: E402
from .notice import Notice, NoticeLayer  # noqa: E402
from .screenshot import Screenshotter  # noqa: E402

HIDE_SETTLE_MS = 700        # box closed -> COSMIC's dock drops its icon and re-centers (~0.5s); wait it out
NUDGE_TIMEOUT_S = 15        # "Need a hand?" goes away by itself (counts as "Not now"), like flippy/mac/nudge.py
TIP_TIMEOUT_S = 25          # a tip card goes away by itself (counts as "Got it")

BASE_CSS = """
window.flippy-overlay, window.flippy-box { background: transparent; }
window.flippy-box entry, window.flippy-box entry:focus-within {
  outline: 0 solid transparent; outline-width: 0; outline-offset: 0; }
window.flippy-box entry > text { border: none; box-shadow: none; background: none; }
window.flippy-box .flippy-card { font-family: "Fira Sans", "Inter", "Noto Sans", sans-serif; font-size: 15px; }
"""

EMPTY_REGION = cairo.Region()
FULL_REGION = cairo.Region(cairo.RectangleInt(0, 0, 100000, 100000))


def layer_window(app, ns, keyboard):
    win = Gtk.Window(application=app)
    win.add_css_class(ns)
    LayerShell.init_for_window(win)
    LayerShell.set_namespace(win, ns)
    LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
    LayerShell.set_keyboard_mode(win, keyboard)
    return win


class Overlay(NoticeLayer, OverlayBase):
    """Permanent full-screen layer surface (see flippy/overlay.py for what's on it).

    Click-through (empty input region) except in draw mode, where the input
    region is widened to the whole surface so it catches the mouse. Only the
    mouse: it never takes keyboard focus (see InputBox for why).
    """

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.keys = None          # KeyCatcher while drawing
        self.win = layer_window(app, "flippy-overlay", LayerShell.KeyboardMode.NONE)
        for e in (LayerShell.Edge.TOP, LayerShell.Edge.BOTTOM, LayerShell.Edge.LEFT, LayerShell.Edge.RIGHT):
            LayerShell.set_anchor(self.win, e, True)
        LayerShell.set_exclusive_zone(self.win, -1)

        self.area = Gtk.DrawingArea()
        self.area.set_draw_func(lambda area, cr, w, h: self.paint(cr, w, h))
        self.win.set_child(self.area)
        self.win.connect("map", lambda w: w.get_surface().set_input_region(EMPTY_REGION))
        self.tick_id = 0
        self.region_key = None    # last input region we set, to avoid resetting it every frame
        self.blur = None          # real glass behind the card (flippy/linux/blur.py), made on the first paint
        self.drag_start = (0, 0)

        drag = Gtk.GestureDrag(button=Gdk.BUTTON_PRIMARY)
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", lambda g, dx, dy: self.motion(self.drag_start[0] + dx, self.drag_start[1] + dy))
        drag.connect("drag-end", lambda *a: self.release())
        self.area.add_controller(drag)
        right = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        right.connect("pressed", lambda *a: self.right_click())
        self.win.add_controller(right)
        self.win.present()

    def paint(self, cr, w, h):
        """Real glass first, where the compositor can blur (flippy/linux/blur.py): the tint under the card and the glass
        pointers, then everything else on top, drawn as for real glass (has_backdrop)."""
        if self.blur is None:
            from .blur import Blur
            self.blur = Blur(self.win.get_display(), self.win.get_surface())
        self.has_backdrop = self.blur.available and settings.get("look", "frosted")
        if self.has_backdrop:
            self._glass(cr, w, h)
        elif self.blur.available:
            self.blur.set_shapes([])  # Frosted glass turned off: no blur anywhere
        super().paint(cr, w, h)

    def _glass(self, cr, w, h):
        from .. import glass
        shapes = []
        lay = self.card_layout(w, h)
        if lay and lay[1]["backdrop"]:
            (x, y, cw, ch), bd = lay[0], self.theme.backdrop
            themes.round_rect(cr, x, y, cw, ch, bd["radius"])
            r, g, b, a = glass.card_tint(bd)
            cr.set_source_rgba(r, g, b, a * lay[1]["card_opacity"])  # Panel opacity: the blur stays, the tint thins
            cr.fill()
            shapes.append(("round", x, y, cw, ch, bd["radius"]))
        lens = self.pointer_lens()
        if lens:
            cx, cy, r = lens
            cr.arc(cx, cy, r, 0, 2 * math.pi)
            cr.set_source_rgba(*glass.glass_rgba(glass.LENS["frost"], glass.LENS["smoke"]))
            cr.fill()
            shapes.append(("circle", cx, cy, r))
        for piece in self.pointer_hand(h) or ():
            themes._piece_path(cr, *piece)
            cr.set_source_rgba(*glass.glass_rgba(glass.LENS["frost"], glass.LENS["smoke"]))
            cr.fill()
            shapes.append(("piece", *piece))
        self.blur.set_shapes(shapes)

    def _drag_begin(self, gesture, x, y):
        self.drag_start = (x, y)
        self.press(x, y)

    def queue_draw(self):
        self.area.queue_draw()

    def set_opacity(self, a):
        self.win.set_opacity(a)

    def start_ticking(self):
        self.tick_id = self.area.add_tick_callback(lambda *a: self.tick() or True)

    def stop_ticking(self):
        if self.tick_id:
            self.area.remove_tick_callback(self.tick_id)
            self.tick_id = 0

    def set_drawing_input(self, on):
        self.win.get_surface().set_input_region(FULL_REGION if on else EMPTY_REGION)
        self.win.set_cursor(Gdk.Cursor.new_from_name("crosshair", None) if on else None)
        if on and self.keys is None:
            self.keys = KeyCatcher(self.app, lambda: self.drawing and self.on_draw_cancel())
        elif not on and self.keys is not None:
            self.keys.close()
            self.keys = None
        if not on:
            self.region_key = None  # let the next frame restore the controls' region

    def hits_changed(self):
        """Clickable = the visible card's controls only. Draw mode manages its own (full) region."""
        if self.drawing or self.in_base_paint:  # NoticeLayer.paint adds the notice's buttons, then calls this
            return
        key = tuple(sorted((n, round(r[0]), round(r[1]), round(r[2]), round(r[3])) for n, r in self.hits.items()))
        if key == self.region_key:
            return
        self.region_key = key
        region = cairo.Region([cairo.RectangleInt(int(rx), int(ry), int(rw) + 1, int(rh) + 1)
                               for rx, ry, rw, rh in self.hits.values()]) if self.hits else EMPTY_REGION
        surface = self.win.get_surface()
        if surface is not None:
            surface.set_input_region(region)
        self.win.set_cursor(Gdk.Cursor.new_from_name("pointer", None) if self.hits else None)


class KeyCatcher:
    """While drawing: a tiny, transparent regular window that holds the keyboard, so Esc cancels.

    The overlay can't take keys itself (KeyboardMode.NONE, see InputBox for why).
    This sits under the overlay, which keeps the mouse, and closing it hands the
    keyboard back to the app underneath, the same way the question box does.
    """

    def __init__(self, app, on_escape):
        self.win = Gtk.Window(application=app, title="Flippy", decorated=False, resizable=False,
                              default_width=1, default_height=1)
        self.win.set_titlebar(Gtk.Box(visible=False))
        self.win.add_css_class("flippy-box")  # transparent
        self.win.set_child(Gtk.Box())
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda c, keyval, code, state: keyval == Gdk.KEY_Escape and (on_escape() or True))
        self.win.add_controller(keys)
        self.win.connect("close-request", lambda w: True)  # only close() closes it
        self.win.present()

    def close(self):
        win, self.win = self.win, None
        GLib.idle_add(lambda: win.destroy())  # not from inside its own event handler


class Nudge:
    """Help mode's and tips' cards, drawn in the overlay's top-right corner (flippy/linux/notice.py).
    Same API as flippy/mac/nudge.py. Each Nudge shows and hides only its own card, so the desktop task's
    approval card (a second Nudge) and a tip can't take each other down."""

    def __init__(self, overlay):
        self.overlay = overlay
        self.timer = 0
        self.notice = None

    @property
    def visible(self):
        return self.notice is not None and self.notice in self.overlay.notices

    def show(self, offer, on_help, on_later, on_mute):
        self.card(offer.headline(), offer.detail(), [("Not now", on_later), ("Help", on_help)],
                  (f"Don't ask in {offer.app_name}", on_mute), on_timeout=on_later)

    def card(self, head, detail, buttons, link=None, on_timeout=None, timeout_s=NUDGE_TIMEOUT_S, width=None):
        """Any choice (or the timeout) closes it, then runs that choice."""
        self.hide()

        def act(fn):
            def go():
                self.hide()
                fn()
            return go
        self.notice = Notice(head, detail, [(t, act(fn)) for t, fn in buttons],
                             (link[0], act(link[1])) if link else None, width)
        self.overlay.show_notice(self.notice)
        if on_timeout:
            self.timer = loop.timeout_add(int(timeout_s * 1000), lambda: act(on_timeout)() and False)

    def press(self, title):
        """Click a button on screen (scripted demos): its title, case-insensitive, or "link"."""
        if not self.visible or self.overlay.notice_card is not self.notice:
            return False
        name = self.overlay.notice_buttons().get(title.lower())
        if name is None:
            return False
        self.overlay.press_notice(name)
        return True

    def hide(self):
        if self.timer:
            loop.source_remove(self.timer)
            self.timer = 0
        if self.notice is not None:
            self.overlay.hide_notice(self.notice)
            self.notice = None


class InputBox:
    """Regular (xdg_toplevel) window, created on show and destroyed on hide.

    Not a layer surface on purpose: on COSMIC a mapped layer surface can't
    give keyboard focus back (switching it to KeyboardMode.NONE keeps eating
    keys), and unmapping one kills the connection. A normal window closes
    cleanly and focus returns to the app underneath.
    """

    def __init__(self, app, on_submit, on_cancel):
        self.app = app
        self.on_submit = on_submit
        self.on_cancel = on_cancel
        self.win = None

    @property
    def visible(self):
        return self.win is not None

    def show(self):
        if self.win:
            self.win.present()
            return
        PROMPTS.reset()
        theme = themes.get(settings.get("look", "theme"))
        win = Gtk.Window(application=self.app, title="Flippy", decorated=False, resizable=False)
        win.set_titlebar(Gtk.Box(visible=False))  # client-side "no titlebar", so COSMIC doesn't add its own
        win.add_css_class("flippy-box")
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.add_css_class("flippy-card")
        card.set_size_request(560, -1)
        entry = Gtk.Entry(placeholder_text=theme.placeholder)
        hint = Gtk.Label(label="Enter to ask or get it done · ↑ earlier · Esc to close · /new · /settings", xalign=0)
        hint.add_css_class("flippy-hint")
        card.append(entry)
        card.append(hint)
        # no titlebar (see set_titlebar above), so the box itself is the drag handle:
        # grab anywhere outside the text field to move it
        handle = Gtk.WindowHandle(child=card)
        win.set_child(handle)
        entry.connect("activate", lambda e: self.submit(e.get_text()))
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        win.add_controller(keys)
        win.connect("close-request", lambda w: (self.on_cancel(), True)[1])
        self.win = win
        self.entry = entry
        win.present()
        entry.grab_focus()

    def _on_key(self, _ctrl, keyval, _code, _state):
        if keyval == Gdk.KEY_Escape:
            self.on_cancel()
            return True
        if keyval in (Gdk.KEY_Up, Gdk.KEY_Down):  # one line, so Up and Down only ever mean history
            self.recall(older=keyval == Gdk.KEY_Up)
            return True
        return False

    def submit(self, text):
        PROMPTS.add(text)
        self.on_submit(text.strip())

    def recall(self, older):
        current = self.entry.get_text()
        text = PROMPTS.older(current) if older else PROMPTS.newer(current)
        if text is not None:
            self.entry.set_text(text)
            self.entry.set_position(-1)  # cursor at the end, like a shell

    def hide(self):
        if self.win:
            win, self.win = self.win, None
            GLib.idle_add(lambda: win.destroy())  # not from inside its own event handler

    # scripted demos
    def set_text(self, text):
        self.entry.set_text(text)
        self.entry.set_position(-1)

    def activate(self):
        self.entry.emit("activate")


class Platform(AppHost):
    hide_settle_ms = HIDE_SETTLE_MS
    act_catalog, act_prompt = act.CATALOG, act.PROMPT  # /act's tools on COSMIC (flippy/linux/act.py)

    def __init__(self, app):
        self.app = app
        display = Gdk.Display.get_default()
        base = Gtk.CssProvider()
        base.load_from_data(BASE_CSS, -1)
        Gtk.StyleContext.add_provider_for_display(display, base, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.theme_css = Gtk.CssProvider()  # the active theme's question-box styling
        Gtk.StyleContext.add_provider_for_display(display, self.theme_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        self.overlay = Overlay(app)
        self.screen = self.screen_size()  # logical px, for the pointer's worker threads (GDK is main-thread only)
        self.screenshotter = Screenshotter()
        self.nudge = Nudge(self.overlay)
        self.action_card = Nudge(self.overlay)  # desktop tasks' approval cards, separate from tips and updates
        self.clicks = None
        self.tray = None
        self.command = lambda cmd: "not ready"
        self.hold = app.hold()
        Gtk.Window.set_default_icon_name("dev.flippy.daemon")  # flippy/linux/desktop.py installs it

    def start(self, command):
        """The panel icon and the Wayland watcher, once the controller exists."""
        self.command = command
        wl.connection()  # start counting input now, so help mode's first minute has history
        try:
            from .tray import Tray
            self.tray = Tray(self)
        except Exception as e:  # no tray is no reason not to run
            print(f"flippy: panel icon unavailable: {e}", flush=True)
        # setup opens by itself only on install, a new major version or a lost login (flippy/setup_gate.py,
        # decided once the first login check is done)

    # --- video review (flippy/video.py): the window in front, grabbed a few times a second
    def video_window(self):
        conn = wl.connection()
        tl = conn.active() if conn else None
        if tl is None or not tl.app_id:
            return None, "Click your video editor first, so it's the window in front."
        if tl.app_id == wl.SELF_APP_ID:
            return None, "That's Flippy's own window. Click your video editor first."
        return tl, sensors.app_name(tl.app_id)

    def video_frame(self, tl):
        from PIL import Image
        conn = wl.connection()
        if conn is None or tl.ext_id not in conn.toplevels:
            return None  # closed
        img = conn.capture_window(tl)
        if img is None:
            return None
        w, h, stride, mode, data = img
        return Image.frombuffer("RGBA" if mode in ("RGBA", "BGRA") else "RGBX", (w, h), data, "raw", mode, stride, 1)

    def shortcut_problems(self):
        """{name: why it won't work, or None} for flippy/shortcuts.py NAMES: unbound, or keys COSMIC also uses."""
        from .. import shortcuts
        from .setup import SHORTCUTS
        system = [shortcuts.read(p) for p in (*shortcuts.COSMIC_DEFAULTS, shortcuts.COSMIC_SYSTEM)]
        return shortcuts.cosmic_problems(shortcuts.read(SHORTCUTS), system)

    def change_shortcuts(self):
        """Settings, on its Shortcuts page (COSMIC runs them; the page says where to change them)."""
        self.open_app("settings/shortcuts")

    def key_label(self, name):
        """For the tour's cards: the COSMIC shortcut that runs it ("Super+Shift+Space"), or None if there isn't one."""
        from .setup import flippy_shortcuts, pretty_accel
        want = {"ask": ("", "ask"), "draw": ("draw",), "video": ("video",), "pause": ("pause-toggle",)}[name]
        return next((pretty_accel(accel) for accel, args in flippy_shortcuts() if args in want), None)

    def setup_pending(self):
        from .setup import needs_setup
        return needs_setup() or self.setup_win is not None

    def _new_app_window(self):
        """History, setup and settings (flippy/linux/shell_window.py, flippy/shell/window.py AppHost)."""
        from .shell_window import AppWindow  # local: WebKit only loads when asked for

        def caps():
            blur = self.overlay.blur  # made on the overlay's first paint
            return {"blur": None if blur is None else bool(blur.available)}
        return AppWindow(on_close=self._app_closed, command=self.command, log=lambda m: None, app=self.app, caps=caps)

    def ask_goal(self, app_id, name):
        """What do they want to do in this app? Steers its tips (the panel menu's "Set a goal for <app>…")."""
        import gi
        gi.require_version("Adw", "1")
        from gi.repository import Adw
        from .. import tips
        Adw.init()
        deck = tips.Deck.load(app_id)
        dlg = Adw.MessageDialog(heading=f"What do you want to do in {name}?",
                                body="Flippy's tips for this app will be about it. For example: make a drum loop, "
                                     "write a CLI in Rust, mix vocals.")
        dlg.set_application(self.app)
        entry = Gtk.Entry(text=deck.goal if deck else "", activates_default=True)
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("save", "Save")
        dlg.set_default_response("save")
        dlg.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)

        def on_response(d, resp):
            if resp != "save" or not entry.get_text().strip():
                return
            self.flippy.set_goal(app_id, entry.get_text(), name)
            if app_id not in settings.get_list("help", "apps"):
                self.command(f"watch-app {app_id}")
            if settings.get("help", "mode") != "tips":
                settings.set("help", "mode", "tips")  # a goal is for tips: turn them on
        dlg.connect("response", on_response)
        dlg.present()

    # --- help mode (flippy/watch.py) and tips ---
    def sample(self):
        return sensors.sample()

    def show_nudge(self, offer, on_help, on_later, on_mute):
        self.nudge.show(offer, on_help, on_later, on_mute)

    def show_tip(self, app_name, text, got_it, knew, show_me):
        self.nudge.card(f"Tip for {app_name}", text, [("Knew that", knew), ("Show me", show_me), ("Got it", got_it)],
                        on_timeout=got_it, timeout_s=TIP_TIMEOUT_S)

    def hide_nudge(self):
        self.nudge.hide()

    def nudge_visible(self):
        return self.nudge.visible

    def press_nudge(self, title):
        """Scripted demos: a button on the card on screen, a desktop task's approval card included."""
        return self.nudge.press(title) or self.action_card.press(title)

    def show_update(self, rel, install, later):
        import subprocess
        notes = next((ln.strip("-*# ").strip() for ln in rel["notes"].splitlines() if ln.strip("-*# ").strip()),
                     "A new version is ready.")
        self.nudge.card(f"Flippy {rel['version']} is out", notes,
                        [("What's new", lambda: subprocess.Popen(["xdg-open", rel["url"]])), ("Later", later),
                         ("Install", install)])

    # --- tutorials: steps that wait for their click (see sensors.ClickWatcher for how a click is noticed) ---
    def watch_clicks(self, fn):
        self.unwatch_clicks()
        self.clicks = sensors.ClickWatcher(fn, self.scale(), self.screen_size(), lambda: self.overlay.ink)

    def unwatch_clicks(self):
        if self.clicks:
            self.clicks.stop()
            self.clicks = None

    def key_idle_s(self):
        """Seconds since the last input (Wayland has no key-only count; mouse moves count too)."""
        return sensors.input_idle_s()

    # --- desktop tasks (/act): one app's window and controls through AT-SPI (flippy/linux/atspi.py). Keys AT-SPI
    # can't do borrow the window while you pause (flippy/linux/borrow.py); the rest leaves your window alone.
    def app_preflight(self):
        from . import atspi
        atspi.preflight()

    def app_front(self):
        from . import atspi
        return atspi.front_app()

    def app_open(self, name, cancel):
        from . import atspi
        return atspi.open_app(name, cancel)

    def app_look(self, app_id, name, handle):
        from . import atspi
        return atspi.look(app_id, name, handle)

    def app_name(self, app_id, handle):
        """The app's display name for approval cards ("Text Editor"), or None."""
        return sensors.app_name(app_id) if isinstance(app_id, str) and app_id else None

    def act_borrow_note(self, name, args):
        """What the "Flippy is acting" line says while a step waits to borrow the window."""
        from . import atspi
        if name == "key" and args.get("combo") not in atspi.BACKGROUND_KEYS:
            return "needs the window for a moment, waiting for you to pause"
        if name in ("scroll", "drag") or (name == "click" and args.get("count") == 2):
            return "needs the pointer for a moment, waiting for you to pause"
        return None

    def app_act(self, name, args, frame, cancel):
        """press / set_text / focus / type / key / menu / media / app_action / click... in the frame's app. Runs on
        the task's worker thread."""
        from ..actions import ActionError, RetryableActionError
        from . import atspi, keyboard
        target = frame.target
        if cancel.is_set():
            raise ActionError("Task canceled.")
        app, win = atspi.app_and_frame(target)
        handle = target[1]
        if app is None and handle < atspi.NO_PID:
            raise ActionError("The app quit. Start a new /act request.")
        el = frame.elements[args["element"]] if "element" in args else None
        result = None
        if name == "press":
            atspi.press(el[0], el[3])
        elif name == "set_text":
            atspi.set_text(el[0], args["text"])
        elif name == "focus":
            atspi.focus(el[0], handle)
        elif name == "type":
            if not atspi.insert_text(win, handle, args["text"]):
                self._borrow_window(target, lambda: keyboard.type_now(args["text"], cancel), cancel)
        elif name == "key":
            if not atspi.background_key(win, handle, args["combo"]):
                self._borrow_window(target, lambda: keyboard.key(args["combo"]), cancel)
        elif name == "menu":
            if win is None:
                raise RetryableActionError("This app shows Flippy no menus. Use keys or click by position instead.")
            atspi.menu(win, args["path"])
        elif name == "media":
            from . import mpris
            mpris.media(args["action"])
        elif name == "app_action":
            from . import scripts
            want = scripts.app_for(args["action"])
            if want and self.app_name(*target[:2]) != want:
                raise RetryableActionError(f"{args['action']} works on {want}; use_app {want} first.")
            result = scripts.run(args["action"], args["args"])
        elif name in ("click", "scroll", "drag"):
            x, y = frame.to_logical(args["x"], args["y"])
            # a control at that spot is pressed in the background; anything else needs the real pointer
            if name != "click" or args["count"] != 1 or not atspi.press_at(win, *atspi.to_window(target, x, y)):
                if name == "click":
                    act = lambda: self.click(x, y, double=args["count"] == 2, wait=True)  # noqa: E731
                elif name == "scroll":
                    act = lambda: self.scroll(x, y, args["direction"], args["lines"])  # noqa: E731
                else:
                    act = lambda: self.drag(x, y, *frame.to_logical(args["to_x"], args["to_y"]))  # noqa: E731
                self._pointer()  # COSMIC's dialog the first time, before the window is borrowed
                self._borrow_window(target, act, cancel, (x, y), keys=False)
        else:
            raise ActionError("Unsupported desktop action.")
        time.sleep(0.25)  # let the app redraw before the next look
        return result

    def _borrow_window(self, target, act, cancel, point=None, keys=True):
        from ..actions import RetryableActionError
        from .borrow import Borrow
        conn = wl.connection()
        if conn is None or not conn.can_activate() or (keys and not conn.can_type()):
            raise RetryableActionError("COSMIC doesn't let Flippy bring the window forward or type here. Use the "
                                       "app's controls or menus instead.")
        Borrow(conn)(conn.toplevels.get(target[2]), act, cancel, point)

    # --- scripted input (flippy-ask click/move/path/type/key/tap, behind automation.clicks): the keyboard is a
    # virtual one (flippy/linux/keyboard.py), the pointer the real one through the RemoteDesktop portal
    # (flippy/linux/remote.py), which asks once. Commands from flippy-ask run on a thread: that first time, the
    # portal waits for the user to answer its dialog.
    def _pointer(self):
        """The RemoteDesktop session, opened if needed; RetryableActionError (for /act) saying why when there's none."""
        from ..actions import RetryableActionError
        from . import remote
        rd = remote.shared()
        try:
            rd.open()
        except remote.Unavailable as e:
            raise RetryableActionError(f"{e} Use the app's controls, menus or keys instead.") from None
        return rd

    def _scripted(self, fn):
        """Run a flippy-ask pointer command on a thread; its failure goes to the log."""
        def run():
            try:
                result = fn()
            except Exception as e:  # noqa: BLE001 - a script's click failing is reported, never raised
                result = str(e)
            if result not in (None, "ok"):
                print(f"flippy: pointer: {result}", flush=True)
        threading.Thread(target=run, daemon=True).start()
        return "ok"

    def click(self, x, y, double=False, wait=False):
        """A real left click at (x, y) on the screen (logical px, top-left origin)."""
        if not wait:
            return self._scripted(lambda: self.click(x, y, double, wait=True))
        rd = self._pointer()
        rd.move(x, y, self.screen)
        time.sleep(0.05)
        for _ in (1, 2) if double else (1,):
            try:
                rd.button(True)
                time.sleep(0.03)
            finally:
                rd.button(False)  # always let go
            time.sleep(0.06)
        return "ok"

    def move(self, x, y):
        return self._scripted(lambda: self._pointer().move(x, y, self.screen))

    def path(self, points, drag):
        """Along points [(x, y, seconds to the next)], holding the button down when drag."""
        if not points:
            return "empty path"
        return self._scripted(lambda: self._gesture(points, drag))

    def _gesture(self, points, drag):
        rd = self._pointer()
        down = False
        try:
            for i, (x, y, dt) in enumerate(points):
                rd.move(x, y, self.screen)
                if drag and i == 0:
                    time.sleep(0.025)
                    down = True
                    rd.button(True)
                if dt:
                    time.sleep(max(dt, 0.004))
        finally:
            if down:
                rd.button(False)
        return "ok"

    def drag(self, x, y, to_x, to_y):
        return self._gesture([(x + (to_x - x) * i / 24, y + (to_y - y) * i / 24, 0.6 / 24 if i < 24 else 0)
                              for i in range(25)], True)

    def scroll(self, x, y, direction, lines):
        rd = self._pointer()
        rd.move(x, y, self.screen)
        time.sleep(0.025)
        rd.scroll(direction, lines)
        return "ok"

    def type_text(self, text, on_key=None):
        from . import keyboard
        return keyboard.type_text(text, on_key)

    def key(self, combo):
        from . import keyboard
        return keyboard.key(combo)

    def tap(self, mod, times):
        from . import keyboard
        return keyboard.tap(mod, times)

    def start_recording(self, path):
        """Demo recording through the ScreenCast portal (flippy/linux/recorder.py)."""
        if getattr(self, "recorder", None) is None:
            from .recorder import Recorder
            self.recorder = Recorder(self.screenshotter.bus)
        self.recorder.start(path)

    def stop_recording(self):
        if getattr(self, "recorder", None) is not None:
            self.recorder.stop()

    def screenshot_to(self, path):
        """Demo scripts: a screenshot through the portal, moved to path."""
        import shutil

        def done(shot, err):
            if err:
                print(f"flippy: shot: {err}", flush=True)
            else:
                shutil.move(shot, path)
        self.screenshotter.take(done)

    def input_box(self, on_submit, on_cancel):
        return InputBox(self.app, on_submit, on_cancel)

    def apply_theme(self, theme):
        self.theme_css.load_from_data(theme.box_css(), -1)

    def listen(self, path, handle):
        """Serve the flippy-ask socket: one command line in, handle(cmd) -> one reply line out."""
        self.service = Gio.SocketService()
        self.service.add_address(Gio.UnixSocketAddress.new(path), Gio.SocketType.STREAM,
                                 Gio.SocketProtocol.DEFAULT, None)

        def incoming(service, conn, _src):
            stream = Gio.DataInputStream.new(conn.get_input_stream())
            line, _ = stream.read_line_utf8(None)
            reply = handle((line or "").strip() or "ask")
            conn.get_output_stream().write_all((reply + "\n").encode(), None)
            conn.close(None)
            return True
        self.service.connect("incoming", incoming)
        self.service.start()

    def restart(self, full_install=False):
        """Start a fresh daemon (through flippy-ask, after this one's gone) and quit. After an update, refresh the
        app library entry and icon (flippy/linux/desktop.py), instead of rerunning install.sh for full_install:
        its apt step needs sudo, which can't ask from here."""
        import os
        import shlex
        import subprocess
        from . import desktop
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        refresh = os.path.exists(desktop.ENTRY)  # installed by install.sh; cheap, so every restart
        q = shlex.quote(root)
        cmd = f"sleep 1; cd {q} && .venv/bin/python -m flippy.linux.desktop install; " if refresh else "sleep 1; "
        subprocess.Popen(["/bin/sh", "-c", cmd + shlex.quote(os.path.join(root, "bin", "flippy-ask")) + " start"],
                         start_new_session=True)
        self.quit()

    def quit(self):
        self.stop_recording()
        GLib.idle_add(self.app.quit)

    # --- monitor info (v1: single monitor) ---
    def _monitor(self):
        return Gdk.Display.get_default().get_monitors().get_item(0)

    def screen_size(self):
        g = self._monitor().get_geometry()
        return g.width, g.height

    def scale(self):
        mon = self._monitor()
        return mon.get_scale() if hasattr(mon, "get_scale") else float(mon.get_scale_factor())


def run(make_app):
    """Start GTK and call make_app(platform) once it's up."""
    if not LayerShell.is_supported():
        sys.exit("compositor doesn't support wlr-layer-shell (or LD_PRELOAD missing)")
    preload.scrub()  # loaded now; children (apps /act opens, terminals, the Claude CLI) mustn't inherit it
    app = Gtk.Application(application_id="dev.flippy.daemon", flags=Gio.ApplicationFlags.NON_UNIQUE)
    state = {}

    def activate(a):
        if "flippy" in state:
            return
        platform = Platform(a)
        state["flippy"] = platform.flippy = make_app(platform)
        platform.start(state["flippy"].command)
    app.connect("activate", activate)
    app.run(None)
