"""macOS windows: a click-through overlay panel, the question box, screenshots, hotkeys, menu bar icon.

The overlay is a borderless, non-activating NSPanel above everything (menu bar
included) on the main display. The shared painting in flippy/overlay.py draws
into a cairo recording surface; only the painted area is rasterized at the
display's backing scale and blitted, so a mostly empty full-screen overlay
stays cheap. The panel ignores the mouse except over the card's controls
(checked every frame) and in draw mode.

Neither panel activates Flippy, so the app you were in stays frontmost and
gets the keyboard back as soon as the question box closes.
"""
import math
import os
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time

import AppKit
import objc
import Quartz
from AppKit import (NSApp, NSApplication, NSApplicationActivationPolicyAccessory, NSBackingStoreBuffered,
                    NSColor, NSCursor, NSEvent, NSFont, NSFontAttributeName, NSForegroundColorAttributeName,
                    NSImage, NSMenu, NSMenuItem, NSPanel, NSScreen, NSScreenSaverWindowLevel,
                    NSStatusBar, NSTextField, NSTrackingActiveAlways, NSTrackingArea, NSTrackingCursorUpdate,
                    NSTrackingInVisibleRect, NSTrackingMouseMoved, NSVariableStatusItemLength, NSView,
                    NSWindowCollectionBehaviorCanJoinAllSpaces, NSWindowCollectionBehaviorFullScreenAuxiliary,
                    NSWindowCollectionBehaviorIgnoresCycle, NSWindowCollectionBehaviorStationary,
                    NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel)
from Foundation import NSAttributedString, NSMakeRect, NSObject
from PyObjCTools import AppHelper
from Quartz import CGPreflightScreenCaptureAccess, CGRequestScreenCaptureAccess

from .. import loop, settings, themes
from ..history import BOX_HINT, PROMPTS
from ..overlay import OverlayBase
from ..shell.window import AppHost
from . import hotkeys, sensors, statusicon
from .nudge import Nudge
from .cairoview import blit
from .widgets import FlippedView

GLASS = hasattr(AppKit, "NSGlassEffectView")  # macOS 26+
# The Media Player theme's Liquid Glass, per surface (question box, its text field, answer card), 0-1:
# FROST is how milky (white) it is, SMOKE how dark. Both low = clear glass.
FROST = {"box": 0.10, "field": 0.30, "card": 0.03}
SMOKE = {"box": 0.25, "field": 0.40, "card": 0.25}


LENS = {"frost": 0.04, "smoke": 0.06}  # the glass pointer: nearly clear


# the tint colors you can pick for Liquid Glass (Settings > Look > Tint color): deep shades, so white text reads
TINTS = {"smoke": (0, 0, 0), "blue": (0.04, 0.18, 0.62), "purple": (0.30, 0.10, 0.62), "pink": (0.66, 0.10, 0.42),
         "red": (0.66, 0.07, 0.08), "orange": (0.72, 0.30, 0.02), "green": (0.04, 0.44, 0.18),
         "teal": (0.02, 0.40, 0.46)}


def glass_color(frost, smoke, rgb=(0, 0, 0)):
    """Frost over smoke as one tint: white at `frost` on top of `rgb` (black by default) at `smoke`."""
    a = frost + smoke * (1 - frost)
    if not a:
        return NSColor.colorWithWhite_alpha_(0, 0)
    r, g, b = ((frost + smoke * (1 - frost) * c) / a for c in rgb)
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, a)


def user_tint(frost, smoke):
    """A surface's glass tint with the user's Glass tint strength and color on top."""
    smoke += (1 - smoke) * min(max(float(settings.get("look", "glass_tint")), 0.0), 1.0) * 0.8
    return glass_color(frost, smoke, TINTS.get(settings.get("look", "glass_color"), TINTS["smoke"]))


def glass_tint(part):
    return glass_color(FROST[part], SMOKE[part])


def _glass_view(radius, tint):
    v = AppKit.NSGlassEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 10))
    v.setStyle_(AppKit.NSGlassEffectViewStyleClear)
    v.setTintColor_(tint)
    v.setCornerRadius_(radius)
    v.setHidden_(True)
    return v
def event_hook(name, **data):
    """The controller's event log (flippy.daemon.event), without importing it at module load."""
    from ..daemon import event
    event(name, **data)


PLATFORM = None             # the running Platform (for windows that need it, like setup's Restart)
ESC_HOTKEY = 4              # hotkey id for Esc while drawing (1-3: ask/draw/video)
HIDE_SETTLE_MS = 150        # let the window server drop the question box before the screenshot
FRAME_MS = 16


def _rgba(r, g, b, a=1.0):
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, a)


def _hex(h, a=1.0):
    h = h.lstrip("#")
    return _rgba(*(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)), a)


def main_screen():
    return NSScreen.screens()[0]  # the one with the menu bar; single display for now


# ⌘ shortcuts a text field normally gets from the app's Edit menu: (key, shift, option) -> action. Flippy has no
# menu bar (a menu bar icon app), and its question box doesn't activate it, so install_edit_keys() answers them.
EDIT_KEYS = {("a", False, False): "selectAll:", ("c", False, False): "copy:", ("x", False, False): "cut:",
             ("v", False, False): "paste:", ("v", True, True): "pasteAsPlainText:", ("z", False, False): "undo:",
             ("z", True, False): "redo:"}
_edit_monitor = None


def edit_action(event):
    """The Edit action for a ⌘ key event ("copy:"), or None."""
    flags = event.modifierFlags()
    if not flags & AppKit.NSEventModifierFlagCommand or flags & AppKit.NSEventModifierFlagControl:
        return None
    key = str(event.charactersIgnoringModifiers() or "").lower()
    return EDIT_KEYS.get((key, bool(flags & AppKit.NSEventModifierFlagShift),
                          bool(flags & AppKit.NSEventModifierFlagOption)))


def install_edit_keys():
    """⌘A/C/X/V/Z (and ⇧⌘Z, ⌥⇧⌘V) in every Flippy text field: the question box, Settings, setup, the goal prompt.
    A hidden Edit menu isn't enough: macOS only uses an app's menu while it's the active app."""
    global _edit_monitor
    if _edit_monitor is not None:
        return

    def handle(event):
        action = edit_action(event)
        win = NSApp.keyWindow()
        if action and win is not None and isinstance(win.firstResponder(), AppKit.NSText) \
                and NSApp.sendAction_to_from_(action, None, win):
            return None  # done: the event goes no further
        return event
    _edit_monitor = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(AppKit.NSEventMaskKeyDown, handle)


class KeyPanel(NSPanel):
    """A borderless panel that can still take the keyboard (without activating the app)."""

    def canBecomeKeyWindow(self):
        return True


# --------------------------------------------------------------------------- overlay

class OverlayView(NSView):
    def isFlipped(self):
        return True

    def acceptsFirstMouse_(self, event):
        return True

    def drawRect_(self, rect):
        self.owner.render(self)

    def updateTrackingAreas(self):
        objc.super(OverlayView, self).updateTrackingAreas()
        if not getattr(self, "tracking", None):
            opts = NSTrackingActiveAlways | NSTrackingInVisibleRect | NSTrackingCursorUpdate | NSTrackingMouseMoved
            self.tracking = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(self.bounds(), opts, self, None)
            self.addTrackingArea_(self.tracking)

    @objc.python_method
    def _pt(self, event):
        p = self.convertPoint_fromView_(event.locationInWindow(), None)
        return p.x, p.y

    def mouseDown_(self, event):
        self.owner.press(*self._pt(event))

    def mouseDragged_(self, event):
        self.owner.motion(*self._pt(event))

    def mouseUp_(self, event):
        self.owner.release()

    def rightMouseDown_(self, event):
        self.owner.right_click()

    def cursorUpdate_(self, event):
        self.owner.update_cursor()

    def mouseMoved_(self, event):
        self.owner.update_cursor()


def _show(view, on):
    if view.isHidden() == on:
        view.setHidden_(not on)


class Overlay(OverlayBase):
    has_backdrop = GLASS

    def __init__(self):
        super().__init__()
        frame = main_screen().frame()
        self.panel = p = KeyPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel, NSBackingStoreBuffered, False)
        p.setLevel_(NSScreenSaverWindowLevel)  # above the menu bar, so we can point at it
        p.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorStationary
                                 | NSWindowCollectionBehaviorFullScreenAuxiliary | NSWindowCollectionBehaviorIgnoresCycle)
        p.setOpaque_(False)
        p.setBackgroundColor_(NSColor.clearColor())
        p.setHasShadow_(False)
        p.setHidesOnDeactivate_(False)
        p.setIgnoresMouseEvents_(True)
        p.setReleasedWhenClosed_(False)
        bounds = NSMakeRect(0, 0, frame.size.width, frame.size.height)
        root = FlippedView.alloc().initWithFrame_(bounds)
        self.glass = self.lens = self.hand = None
        self.glass_theme = None
        if GLASS:  # Liquid Glass behind the card (Theme.backdrop) and the glass pointer; cairo paints on top
            self.glass = _glass_view(20, glass_tint("card"))
            self.lens = _glass_view(20, glass_color(LENS["frost"], LENS["smoke"]))
            root.addSubview_(self.glass)
            root.addSubview_(self.lens)
            # the glass hand: rounded pieces in a container, which fuses nearby glass into one shape
            self.hand = AppKit.NSGlassEffectContainerView.alloc().initWithFrame_(bounds)
            self.hand.setSpacing_(6)
            hand_view = FlippedView.alloc().initWithFrame_(bounds)
            self.hand_pieces = [_glass_view(4, glass_color(LENS["frost"], LENS["smoke"])) for _ in themes.GLASS_HAND]
            for piece in self.hand_pieces:
                hand_view.addSubview_(piece)
            self.hand.setContentView_(hand_view)
            self.hand.setHidden_(True)
            root.addSubview_(self.hand)
        self.view = OverlayView.alloc().initWithFrame_(bounds)
        self.view.owner = self
        root.addSubview_(self.view)
        p.setContentView_(root)
        p.orderFrontRegardless()
        self.tick_id = 0
        self.ignoring = True

    def size(self):
        b = self.view.bounds()
        return b.size.width, b.size.height

    def render(self, view):
        w, h = self.size()
        blit(lambda cr: self.paint(cr, w, h), w, h, self.panel.backingScaleFactor(), clip_to_ink=True)

    # --- hooks ---
    def queue_draw(self):
        self._place_glass()
        self.view.setNeedsDisplay_(True)

    def _place_glass(self):
        """Keep the glass exactly under the card and the pointer lens (both move every frame)."""
        if self.glass is None:
            return
        lay = self.card_layout(*self.size())
        if lay and lay[1]["backdrop"]:
            theme = self.theme
            key = (theme, settings.get("look", "glass_tint"), settings.get("look", "glass_color"))
            if key != self.glass_theme:  # per-theme tint (Glass uses its own frost/smoke), plus the user's tint
                self.glass_theme = key
                bd = theme.backdrop
                frost, smoke = (bd["frost"], bd["smoke"]) if "frost" in bd else (FROST["card"], SMOKE["card"])
                self.glass.setTintColor_(user_tint(frost, smoke))
                self.glass.setCornerRadius_(bd["radius"])
            self.glass.setFrame_(NSMakeRect(*lay[0]))
            _show(self.glass, True)
        else:
            _show(self.glass, False)
        pieces = self.pointer_hand(self.size()[1])
        if pieces:
            for view, (px, py, w, h, r, rot) in zip(self.hand_pieces, pieces):
                view.setFrameCenterRotation_(0)
                view.setFrame_(NSMakeRect(px, py, w, h))
                view.setCornerRadius_(r)
                view.setFrameCenterRotation_(rot)  # in a flipped parent this turns the same way as cairo
                _show(view, True)
        _show(self.hand, bool(pieces))
        lens = self.pointer_lens()
        if lens:
            cx, cy, r = lens
            self.lens.setFrame_(NSMakeRect(cx - r, cy - r, 2 * r, 2 * r))
            self.lens.setCornerRadius_(r)
            _show(self.lens, True)
        else:
            _show(self.lens, False)

    def set_opacity(self, a):
        self.panel.setAlphaValue_(a)

    def start_ticking(self):
        self.tick_id = loop.timeout_add(FRAME_MS, lambda: self.tick() or True)

    def stop_ticking(self):
        if self.tick_id:
            loop.source_remove(self.tick_id)
            self.tick_id = 0

    def set_drawing_input(self, on):
        self._set_ignoring(not on)
        self.update_cursor()
        # the overlay never takes the keyboard, so Esc is a global hotkey, only while drawing
        if on:
            err = hotkeys.register(ESC_HOTKEY, "escape", lambda: (event_hook("hotkey", key="escape"),
                                                                   self.on_draw_cancel()))
            if err:
                print(f"flippy: Esc while drawing: {err}", flush=True)
        else:
            hotkeys.unregister(ESC_HOTKEY)

    def _mouse(self):
        """Mouse position in overlay (flipped, logical) coordinates."""
        p, f = NSEvent.mouseLocation(), self.panel.frame()
        return p.x - f.origin.x, f.origin.y + f.size.height - p.y

    def _over_control(self):
        x, y = self._mouse()
        return any(rx <= x <= rx + rw and ry <= y <= ry + rh for rx, ry, rw, rh in self.hits.values())

    def hits_changed(self):
        """Take clicks only while the mouse is over a control (or dragging a slider)."""
        if not self.drawing:
            self._set_ignoring(not (self.slider or (self.hits and self._over_control())))

    def _set_ignoring(self, ignore):
        if ignore != self.ignoring:
            self.ignoring = ignore
            self.panel.setIgnoresMouseEvents_(ignore)
            self.update_cursor()

    def update_cursor(self):
        if self.drawing:
            NSCursor.crosshairCursor().set()
        elif not self.ignoring:
            NSCursor.pointingHandCursor().set()
        else:
            NSCursor.arrowCursor().set()


# --------------------------------------------------------------------------- question box

def box_style(theme):
    """The question box's look per theme (the macOS counterpart of Theme.box_css)."""
    st = {"bg": _rgba(24 / 255, 24 / 255, 28 / 255, 0.94), "border": _rgba(90 / 255, 160 / 255, 1, 0.55),
          "border_w": 1, "radius": 14, "fg": _hex("f2f2f2"), "font": (None, 16), "field_bg": None,
          "field_border": None, "field_radius": 0, "hint": _rgba(1, 1, 1, 0.5), "hint_font": (None, 12)}
    if theme.key == "mono":
        st.update(bg=_rgba(0, 0, 0, 0.96), border=_rgba(*theme.outline), radius=0, fg=_rgba(*theme.fg),
                  field_bg=_rgba(0, 0, 0, 1), field_border=_rgba(*theme.outline, 0.7), field_radius=0,
                  hint=_rgba(*theme.fg, 0.55))
    elif theme.key == "cosmic":
        st.update(bg=_rgba(*theme.bg, 0.96), border=_rgba(*theme.accent, 0.7), radius=theme.radius,
                  fg=_rgba(*theme.fg), hint=_rgba(*theme.fg, 0.5))
    elif theme.key == "terminal":
        green = (64 / 255, 1, 115 / 255)
        st.update(bg=_hex("030805"), border=_rgba(*green, 0.6), radius=0, fg=_rgba(*green), font=("Menlo", 16),
                  field_bg=_hex("000000"), field_border=_rgba(*green, 0.35), hint=_rgba(*green, 0.55),
                  hint_font=("Menlo", 12))
    elif theme.key == "mediaplayer" and GLASS:  # Liquid Glass box; the field is a frostier, smokier pane
        st.update(glass=True, bg=None, border=_rgba(1, 1, 1, 0.35), radius=theme.LIQUID_R, fg=_hex("ffffff"),
                  field_bg=glass_tint("field"), field_border=_rgba(1, 1, 1, 0.3),
                  field_radius=12, hint=_rgba(1, 1, 1, 0.9), hint_shadow=True)
    elif theme.key == "glass" and GLASS:  # same glass box, the lock screen's rounder corners
        st.update(glass=True, bg=None, border=_rgba(1, 1, 1, 0.3), radius=theme.R, fg=_hex("ffffff"),
                  field_bg=_rgba(1, 1, 1, 0.12), field_radius=12, hint=_rgba(1, 1, 1, 0.75), hint_shadow=True)
    elif theme.key == "glass":  # (no Liquid Glass on this macOS)
        st.update(bg=_rgba(40 / 255, 40 / 255, 46 / 255, 0.85), border=_rgba(1, 1, 1, 0.3), radius=theme.R,
                  field_bg=_rgba(1, 1, 1, 0.12), field_radius=12)
    elif theme.key == "mediaplayer":
        st.update(bg=_rgba(40 / 255, 46 / 255, 48 / 255, 0.88), border=_rgba(0, 0, 0, 0.7), radius=9, fg=_hex("ffffff"),
                  font=(None, 15), field_bg=_rgba(0, 0, 0, 0.3), field_border=_rgba(0, 0, 0, 0.6), field_radius=12,
                  hint=_rgba(235 / 255, 238 / 255, 240 / 255, 0.7))
    return st


def _font(spec):
    name, size = spec
    return (NSFont.fontWithName_size_(name, size) if name else None) or NSFont.systemFontOfSize_(size)


def _layer(view, bg, border=None, border_w=1, radius=0):
    view.setWantsLayer_(True)
    layer = view.layer()
    layer.setBackgroundColor_(bg.CGColor() if bg else None)
    if border:
        layer.setBorderColor_(border.CGColor())
        layer.setBorderWidth_(border_w)
    layer.setCornerRadius_(radius)


class BoxDelegate(NSObject, protocols=[objc.protocolNamed("NSTextFieldDelegate")]):
    def control_textView_doCommandBySelector_(self, control, text_view, sel):
        sel = sel.decode() if isinstance(sel, bytes) else str(sel)
        if sel == "insertNewline:":
            self.box.submit(str(control.stringValue()))
            return True
        if sel == "cancelOperation:":
            self.box.on_cancel()
            return True
        if sel in ("moveUp:", "moveDown:"):  # one line, so Up and Down only ever mean history
            self.box.recall(older=sel == "moveUp:")
            return True
        return False


class InputBox:
    WIDTH = 560
    PAD_X, PAD_Y = 16, 12

    def __init__(self, on_submit, on_cancel):
        self.on_submit = on_submit
        self.on_cancel = on_cancel
        self.panel = None
        self.delegate = BoxDelegate.alloc().init()
        self.delegate.box = self

    @property
    def visible(self):
        return self.panel is not None

    def submit(self, text):
        PROMPTS.add(text)
        self.on_submit(text.strip())

    def recall(self, older):
        current = str(self.entry.stringValue())
        text = PROMPTS.older(current) if older else PROMPTS.newer(current)
        if text is None:
            return
        self.entry.setStringValue_(text)
        editor = self.entry.currentEditor()
        if editor is not None:
            editor.setSelectedRange_((len(text), 0))  # cursor at the end, like a shell

    def show(self, hint=BOX_HINT):
        """hint: the line under the field (a follow-up's says Esc goes back to the walkthrough)."""
        if self.panel:
            self.hint.setStringValue_(hint)
            self.panel.makeKeyAndOrderFront_(None)
            return
        PROMPTS.reset()
        theme = themes.get(settings.get("look", "theme"))
        st = box_style(theme)
        font, hint_font = _font(st["font"]), _font(st["hint_font"])
        field_h = math.ceil(font.ascender() - font.descender() + font.leading()) + 12
        hint_h = math.ceil(hint_font.ascender() - hint_font.descender()) + 2
        w = self.WIDTH + 2 * self.PAD_X
        h = self.PAD_Y + field_h + 6 + hint_h + self.PAD_Y

        frame = main_screen().visibleFrame()
        x = frame.origin.x + (frame.size.width - w) / 2
        y = frame.origin.y + frame.size.height * 0.72 - h
        panel = KeyPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, w, h), NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered, False)
        panel.setLevel_(NSScreenSaverWindowLevel + 1)  # over the overlay, so a circle you drew never crosses it
        panel.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces
                                     | NSWindowCollectionBehaviorFullScreenAuxiliary)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(True)
        panel.setMovableByWindowBackground_(True)  # drag the box anywhere outside the text field
        panel.setReleasedWhenClosed_(False)
        panel.setBecomesKeyOnlyIfNeeded_(False)

        card = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        _layer(card, st["bg"], st["border"], st["border_w"], st["radius"])
        root = card
        if st.get("glass"):
            root = AppKit.NSGlassEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
            root.setStyle_(AppKit.NSGlassEffectViewStyleClear)
            root.setTintColor_(user_tint(FROST["box"], SMOKE["box"]))
            root.setCornerRadius_(st["radius"])
            root.setContentView_(card)
            panel.setHasShadow_(False)  # the glass draws its own edge and depth
        field_box = NSView.alloc().initWithFrame_(NSMakeRect(self.PAD_X, h - self.PAD_Y - field_h, self.WIDTH, field_h))
        _layer(field_box, st["field_bg"], st["field_border"], 1, st["field_radius"])
        inset = 8 if st["field_bg"] else 0
        entry = NSTextField.alloc().initWithFrame_(NSMakeRect(inset, 6, self.WIDTH - 2 * inset, field_h - 12))
        entry.setBezeled_(False)
        entry.setBordered_(False)
        entry.setDrawsBackground_(False)
        entry.setFocusRingType_(1)  # NSFocusRingTypeNone
        entry.setFont_(font)
        entry.setTextColor_(st["fg"])
        entry.setPlaceholderAttributedString_(NSAttributedString.alloc().initWithString_attributes_(
            theme.placeholder, {NSFontAttributeName: font, NSForegroundColorAttributeName: st["fg"].colorWithAlphaComponent_(0.45)}))
        entry.cell().setUsesSingleLineMode_(True)
        entry.cell().setScrollable_(True)
        entry.setDelegate_(self.delegate)
        field_box.addSubview_(entry)
        hint = NSTextField.labelWithString_(hint)
        hint.setFont_(hint_font)
        hint.setTextColor_(st["hint"])
        if st.get("hint_shadow"):  # white hint on light frost: a soft shadow keeps it readable
            shadow = AppKit.NSShadow.alloc().init()
            shadow.setShadowColor_(NSColor.colorWithWhite_alpha_(0, 0.6))
            shadow.setShadowBlurRadius_(3)
            shadow.setShadowOffset_((0, -1))
            hint.setShadow_(shadow)
        hint.setFrame_(NSMakeRect(self.PAD_X, self.PAD_Y - 2, self.WIDTH, hint_h))
        card.addSubview_(field_box)
        card.addSubview_(hint)
        panel.setContentView_(root)

        self.panel, self.entry, self.hint = panel, entry, hint
        panel.makeKeyAndOrderFront_(None)
        panel.makeFirstResponder_(entry)
        editor = entry.currentEditor()
        if editor is not None:
            editor.setInsertionPointColor_(st["fg"])
            editor.setAllowsUndo_(True)  # ⌘Z / ⇧⌘Z

    def hide(self):
        if self.panel:
            panel, self.panel = self.panel, None
            panel.orderOut_(None)
            AppHelper.callAfter(panel.close)  # not from inside its own event handler

    # scripted demos
    def set_text(self, text):
        """Scripted typing: append what's new through the field editor, like keystrokes (no select-all flash)."""
        current = str(self.entry.stringValue())
        editor = self.entry.currentEditor()
        if editor is not None and text.startswith(current):
            editor.insertText_(text[len(current):])
        else:
            self.entry.setStringValue_(text)

    def activate(self):
        self.submit(str(self.entry.stringValue()))


# --------------------------------------------------------------------------- screenshots

SCREEN_PERMISSION = ("Flippy needs Screen Recording permission. Turn it on in System Settings → Privacy & Security "
                     "→ Screen & System Audio Recording, then restart Flippy.")


class Screenshotter:
    def take(self, on_done, timeout_s=15):
        """Calls on_done(path, None) on success or on_done(None, error_str), on the main thread."""
        if not CGPreflightScreenCaptureAccess():
            CGRequestScreenCaptureAccess()  # shows the system prompt the first time
            loop.idle_add(on_done, None, SCREEN_PERMISSION)
            return
        fd, path = tempfile.mkstemp(prefix="flippy-", suffix=".png")
        os.close(fd)

        def run():
            try:
                r = subprocess.run(["/usr/sbin/screencapture", "-x", "-m", "-t", "png", path],
                                   capture_output=True, timeout=timeout_s)
                ok = r.returncode == 0 and os.path.getsize(path) > 0
                err = None if ok else (r.stderr.decode().strip() or f"screencapture exited {r.returncode}")
            except subprocess.TimeoutExpired:
                err = "screenshot timed out"
            if err:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            loop.idle_add(on_done, None if err else path, err)
        threading.Thread(target=run, daemon=True).start()


# --------------------------------------------------------------------------- menu bar

class MenuTarget(NSObject, protocols=[objc.protocolNamed("NSMenuDelegate")]):
    def act_(self, sender):
        cmd = str(sender.representedObject())
        p = self.platform
        if cmd == "setup":
            p.open_setup()
        elif cmd == "history":
            p.open_app("history")
        elif cmd == "help-toggle":
            p.command(f"help-mode {'off' if settings.get('help', 'mode') != 'off' else 'quiet'}")
        elif cmd == "tips-toggle":
            p.command(f"help-mode {'quiet' if settings.get('help', 'mode') == 'tips' else 'tips'}")
        elif cmd == "goal-front":
            p.ask_goal()
        elif cmd == "watch-front":
            if p.menu_front:
                p.command(f"watch-app {p.menu_front[0]}")
        else:
            p.command(cmd)

    def menuWillOpen_(self, menu):
        self.platform.refresh_help_menu()


MENU = [("Ask about the screen", "ask", "ask"), ("Circle and ask", "draw", "draw"),
        ("Check a video edit", "video", "video"), None,
        ("Help when I'm stuck", "help-toggle", None), ("Tips while I work", "tips-toggle", None),
        ("Watch this app", "watch-front", None), ("Set a goal…", "goal-front", None), None,
        ("Preview the look", "preview", None), ("History…", "history", None), ("Settings…", "settings", None),
        ("New session", "reset", None),
        None, ("Check for updates…", "update", None), ("Setup…", "setup", None), ("Take the tour", "tour", None),
        ("Quit Flippy", "quit", None)]


# --------------------------------------------------------------------------- platform

class Platform(AppHost):
    hide_settle_ms = HIDE_SETTLE_MS

    def __init__(self):
        self.overlay = Overlay()
        self.screenshotter = Screenshotter()
        self.nudge = Nudge()
        self.action_card = Nudge()  # separate from tips and update cards
        self.double_tap = hotkeys.DoubleTap()
        self.command = lambda cmd: "not ready"

    def input_box(self, on_submit, on_cancel):
        return InputBox(on_submit, on_cancel)

    def apply_theme(self, theme):
        pass  # the question box reads box_style() each time it opens

    def start(self, command):
        """Hotkeys and the menu bar icon, once the controller exists."""
        self.command = command
        from . import ax
        ax.watch_front()
        self._bind_keys()
        if not hotkeys.accessibility_trusted():
            # The double-tap pause needs Accessibility; once it's granted, bind it without a restart.
            def trust_granted():
                if not hotkeys.accessibility_trusted():
                    return True
                self._bind_keys()
                return False
            loop.timeout_add(2000, trust_granted)
        settings.on_change(lambda section, key, value: section == "keys" and self._bind_keys())
        self._menu_bar()
        # setup opens by itself only on install, a new major version or a lost login (flippy/setup_gate.py,
        # decided once the first login check is done)

    def _new_app_window(self):
        """History, setup and settings (flippy/mac/shell_window.py, flippy/shell/window.py AppHost)."""
        from .shell_window import AppWindow  # local: WebKit only loads when asked for
        return AppWindow(on_close=self._app_closed, command=self.command, log=lambda m: None)

    def _app_closed(self):
        from . import ax
        super()._app_closed()
        ax.give_back_front()

    def _bind_keys(self):
        self.key_errors = {}
        for hid, name in ((1, "ask"), (2, "draw"), (3, "video")):
            err = hotkeys.register(hid, settings.get("keys", name), lambda n=name: self.command(f"hotkey {n}"))
            self.key_errors[name] = err
            if err:
                print(f"flippy: hotkey for {name}: {err}", flush=True)
        pause = settings.get("keys", "pause")
        err = self.double_tap.set(None if pause == "off" else pause[len("double-"):],
                                  lambda: self.command("hotkey pause"))
        self.key_errors["pause"] = err
        if err:
            print(f"flippy: pause shortcut: {err}", flush=True)
        if getattr(self, "menu_items", None):
            self._label_menu()

    def _menu_bar(self):
        self.status = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self._status_icon()
        settings.on_change(lambda section, key, value: section == "look" and key in ("pointer", "theme")
                           and self._status_icon())  # the icon is the equipped pointer
        self.target = MenuTarget.alloc().init()
        self.target.platform = self
        menu = NSMenu.alloc().init()
        self.menu_items = {}
        self.help_items = {}
        self.menu_front = None
        for entry in MENU:
            if entry is None:
                menu.addItem_(NSMenuItem.separatorItem())
                continue
            title, cmd, key = entry
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, "act:", "")
            item.setTarget_(self.target)
            item.setRepresentedObject_(cmd)
            menu.addItem_(item)
            if key:
                self.menu_items[key] = (item, title)
            if cmd in ("help-toggle", "tips-toggle", "watch-front", "goal-front"):
                self.help_items[cmd] = item
        menu.setDelegate_(self.target)
        self.status.setMenu_(menu)
        self._label_menu()

    def refresh_help_menu(self):
        """The menu is opening: the app in front is the one they're working in (Flippy never activates)."""
        self._label_menu()  # a shortcut can break or get fixed while Flippy runs (Accessibility, a new app)
        app, name, pid = sensors.frontmost()
        self.menu_front = (app, name) if app and pid != os.getpid() else None
        mode = settings.get("help", "mode")
        self.help_items["help-toggle"].setState_(1 if mode != "off" else 0)
        self.help_items["tips-toggle"].setState_(1 if mode == "tips" else 0)
        watch_item, goal_item = self.help_items["watch-front"], self.help_items["goal-front"]
        if self.menu_front:
            watched = self.menu_front[0] in settings.get_list("help", "apps")
            watch_item.setTitle_(f"Watch {self.menu_front[1]}")
            watch_item.setState_(1 if watched else 0)
            goal_item.setTitle_(f"Set a goal for {self.menu_front[1]}…")
        else:
            watch_item.setTitle_("Watch this app")
            watch_item.setState_(0)
            goal_item.setTitle_("Set a goal…")
        watch_item.setEnabled_(bool(self.menu_front))
        goal_item.setEnabled_(bool(self.menu_front))

    def ask_goal(self):
        """What do they want to do in this app? Steers its tips."""
        if not self.menu_front:
            return
        app, name = self.menu_front
        from .. import tips
        deck = tips.Deck.load(app)
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_(f"What do you want to do in {name}?")
        alert.setInformativeText_("Flippy's tips for this app will be about it. For example: make a drum loop, "
                                  "write a CLI in Rust, mix vocals.")
        field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 300, 24))
        field.setStringValue_(deck.goal if deck else "")
        alert.setAccessoryView_(field)
        alert.addButtonWithTitle_("Save")
        alert.addButtonWithTitle_("Cancel")
        NSApp.activateIgnoringOtherApps_(True)
        alert.window().setInitialFirstResponder_(field)
        if alert.runModal() == AppKit.NSAlertFirstButtonReturn:
            self.flippy.set_goal(app, str(field.stringValue()), name)
            if app not in settings.get_list("help", "apps"):
                self.command(f"watch-app {app}")
            if settings.get("help", "mode") != "tips":
                settings.set("help", "mode", "tips")  # a goal is for tips: turn them on

    # --- video review (flippy/video.py): the window in front, grabbed a few times a second ---
    def video_window(self):
        """(window id, app name) of the frontmost app's front window, or (None, why not)."""
        app, name, pid = sensors.frontmost()
        if pid == os.getpid():
            return None, "That's Flippy's own window. Click your video editor first."
        wid = sensors.front_window(pid) if pid else None
        if wid is None:
            return None, "Click your video editor first, so it's the window in front."
        return wid, name

    def video_frame(self, wid):
        """That one window as a PIL image (nothing above it, so Flippy's cards stay out), or None if it closed.
        Called from the recording thread."""
        from PIL import Image
        from .ax import _capture
        img = _capture(wid)
        if img is not None:
            return img
        if not sensors.window_exists(wid):
            return None
        # CGWindowListCreateImage is deprecated (macOS 14+): if it stops returning frames, screencapture still can
        fd, path = tempfile.mkstemp(prefix="flippy-video-", suffix=".png")
        os.close(fd)
        try:
            r = subprocess.run(["/usr/sbin/screencapture", "-x", "-o", "-l", str(wid), "-t", "png", path],
                               capture_output=True, timeout=5)
            if r.returncode != 0 or os.path.getsize(path) == 0:
                return None
            with Image.open(path) as im:
                return im.convert("RGB")
        finally:
            os.unlink(path)

    # --- help mode (flippy/watch.py) ---
    def sample(self):
        return sensors.sample()

    def show_nudge(self, offer, on_help, on_later, on_mute):
        self.nudge.show(offer, on_help, on_later, on_mute)

    def show_tip(self, app_name, text, got_it, knew, show_me):
        self.nudge.card(f"Tip for {app_name}", text, [("Knew that", knew), ("Show me", show_me), ("Got it", got_it)],
                        on_timeout=got_it, timeout_s=25)

    def hide_nudge(self):
        self.nudge.hide()

    def nudge_visible(self):
        return self.nudge.visible

    def _status_icon(self):
        try:
            img = statusicon.image()
        except Exception as e:  # never lose the menu bar icon over a drawing problem
            print(f"flippy: menu bar icon: {e}", flush=True)
            img = NSImage.imageWithSystemSymbolName_accessibilityDescription_("cursorarrow.rays", "Flippy")
        img.setAccessibilityDescription_("Flippy")
        self.status.button().setImage_(img)

    def shortcut_problems(self):
        """{name: why it won't work, or None} for flippy/shortcuts.py NAMES (the tour, the menu)."""
        from .. import shortcuts
        errors = getattr(self, "key_errors", {})
        system = shortcuts.system_shortcuts()
        out = {name: shortcuts.mac_problem(settings.get("keys", name), hotkeys.parse, errors.get(name), system)
               for name in ("ask", "draw", "video")}
        pause = settings.get("keys", "pause")
        out["pause"] = None if pause == "off" else errors.get("pause") or (
            None if hotkeys.accessibility_trusted() else "double-tap shortcuts need the Accessibility permission")
        return out

    def change_shortcuts(self):
        """Where to change them: Settings, on its Shortcuts page."""
        self.open_app("settings/shortcuts")

    def key_label(self, name):
        """For the tour's cards: "⇧⌘Space", "double-tap ⌘", or None if it's off."""
        combo = settings.get("keys", name)
        if combo == "off":
            return None
        if combo.startswith("double-"):
            return f"double-tap {hotkeys.SYMBOLS.get(combo[len('double-'):], '?')}"
        return hotkeys.pretty(combo)

    def setup_pending(self):
        from .setup import needs_setup
        return needs_setup() or self.setup_win is not None

    def _label_menu(self):
        problems = self.shortcut_problems()
        for key, (item, title) in self.menu_items.items():
            warn = problems.get(key)
            item.setTitle_(f"{title}    {hotkeys.pretty(settings.get('keys', key))}" + ("  ⚠" if warn else ""))
            item.setToolTip_(f"This shortcut won't work: {warn}. Change it in Settings → Hotkeys." if warn else None)

    def listen(self, path, handle):
        """Serve the flippy-ask socket: one command line in, handle(cmd) -> one reply line out."""
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(8)

        def on_main(line):
            done, out = threading.Event(), {}

            def run():
                try:
                    out["reply"] = handle(line)
                except Exception as e:  # keep serving
                    out["reply"] = f"error: {e}"
                done.set()
            AppHelper.callAfter(run)
            done.wait(10)
            return out.get("reply", "timeout")

        def serve():
            while True:
                conn, _ = srv.accept()
                with conn:
                    conn.settimeout(3)
                    data = b""
                    try:
                        while b"\n" not in data and len(data) < 65536:
                            chunk = conn.recv(4096)
                            if not chunk:
                                break
                            data += chunk
                        line = data.decode(errors="replace").strip() or "ask"
                        conn.sendall((on_main(line) + "\n").encode())
                    except OSError:
                        pass
        threading.Thread(target=serve, daemon=True).start()

    # --- demo recording (scripts/demo_mac.py): run as Flippy's children, so they use its Screen Recording ---
    def screenshot_to(self, path):
        subprocess.run(["/usr/sbin/screencapture", "-x", "-m", "-t", "png", path], timeout=15)

    def start_recording(self, path):
        """Record the main display with macOS's own screencapture (screen only: no cameras, no mic).
        path + '.json' gets the wall clock at stop; the first frame is then stop - the video's duration."""
        self.stop_recording()
        self.recorder = subprocess.Popen(["/usr/sbin/screencapture", "-v", "-C", "-x", "-D1", path],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.recording = path

    def stop_recording(self):
        rec = getattr(self, "recorder", None)
        if rec and rec.poll() is None:
            import signal
            stopped = time.time()
            rec.send_signal(signal.SIGINT)  # screencapture's own stop: finishes the file
            try:
                rec.wait(30)
            except subprocess.TimeoutExpired:
                rec.kill()
            with open(self.recording + ".json", "w") as f:
                json.dump({"stopped": stopped}, f)
        self.recorder = None

    def click(self, x, y, double=False):
        """Post a real left click at (x, y) on the main display (logical px, top-left origin)."""
        if not Quartz.CGPreflightPostEventAccess():
            Quartz.CGRequestPostEventAccess()  # the system prompt, the first time
            return ("Flippy needs the Accessibility permission to click: System Settings > Privacy & Security > "
                    "Accessibility, turn on Flippy, then restart it")
        self._check_physical_input(mouse=True)
        pt = Quartz.CGPointMake(x, y)
        move = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, pt, Quartz.kCGMouseButtonLeft)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, move)
        time.sleep(0.05)
        for n in (1, 2) if double else (1,):
            try:
                ev = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, pt, Quartz.kCGMouseButtonLeft)
                Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventClickState, n)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                time.sleep(0.03)
            finally:
                try:
                    ev = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, pt, Quartz.kCGMouseButtonLeft)
                    Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventClickState, n)
                    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                except Exception:
                    from ..actions import InputCleanupError
                    raise InputCleanupError("Input release could not be confirmed. Restart Flippy before acting again.") from None
        return "ok"

    def key_idle_s(self):
        """Seconds since the last key press anywhere (an event counter: no keylogging, no permission)."""
        return Quartz.CGEventSourceSecondsSinceLastEventType(Quartz.kCGEventSourceStateCombinedSessionState,
                                                             Quartz.kCGEventKeyDown)

    def watch_clicks(self, fn):
        """fn(x, y) on every left click in other apps (logical px, top-left origin). No permission needed."""
        self.unwatch_clicks()

        def on_click(event):
            p, f = NSEvent.mouseLocation(), main_screen().frame()
            fn(p.x - f.origin.x, f.origin.y + f.size.height - p.y)
        self.click_monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(AppKit.NSEventMaskLeftMouseDown,
                                                                                  on_click)

    def unwatch_clicks(self):
        if getattr(self, "click_monitor", None) is not None:
            NSEvent.removeMonitor_(self.click_monitor)
            self.click_monitor = None

    # --- scripted input (flippy-ask move/path/type/key/tap, behind automation.clicks): real events, so the
    # cursor and keystrokes look like a person's in recordings. Long gestures run on a thread.
    def _can_post(self):
        if Quartz.CGPreflightPostEventAccess():
            return None
        Quartz.CGRequestPostEventAccess()
        return ("Flippy needs the Accessibility permission for this: System Settings > Privacy & Security > "
                "Accessibility, turn on Flippy, then restart it")

    @staticmethod
    def _mouse(kind, x, y, clicks=1):
        ev = Quartz.CGEventCreateMouseEvent(None, kind, Quartz.CGPointMake(x, y), Quartz.kCGMouseButtonLeft)
        Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventClickState, clicks)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)

    def move(self, x, y):
        return self._can_post() or self._mouse(Quartz.kCGEventMouseMoved, x, y) or "ok"

    def _gesture(self, points, drag, modifiers=()):
        """Synchronous worker body; completion acknowledges release of owned input."""
        if drag and Quartz.CGEventSourceButtonState(Quartz.kCGEventSourceStateCombinedSessionState, Quartz.kCGMouseButtonLeft):
            raise RuntimeError("Release the physical mouse button before dragging.")
        down = False
        owned = []
        position = points[0][:2]
        flags = 0
        try:
            for mod in modifiers:
                code = self.MOD_KEYS[mod]
                if Quartz.CGEventSourceKeyState(Quartz.kCGEventSourceStateCombinedSessionState, code):
                    raise RuntimeError("Release physical modifiers before dragging.")
                owned.append(mod)
                flags |= self.FLAGS[mod]
                self._modifier(mod, flags)
            for i, (x, y, dt) in enumerate(points):
                position = x, y
                if drag and i == 0:
                    self._mouse(Quartz.kCGEventMouseMoved, x, y)
                    time.sleep(0.025)
                    down = True  # release even if posting reports a failure after delivery
                    self._gesture_mouse(Quartz.kCGEventLeftMouseDown, x, y, flags)
                else:
                    self._gesture_mouse(Quartz.kCGEventLeftMouseDragged if down else Quartz.kCGEventMouseMoved,
                                        x, y, flags)
                if dt:
                    time.sleep(max(dt, 0.004))
            return "ok"
        finally:
            release_error = None
            if down:
                try:
                    self._gesture_mouse(Quartz.kCGEventLeftMouseUp, *position, flags)
                except Exception as err:
                    release_error = err
            for mod in reversed(owned):
                flags &= ~self.FLAGS[mod]
                try:
                    self._modifier(mod, flags)
                except Exception as err:
                    release_error = release_error or err
            if release_error:
                from ..actions import InputCleanupError
                raise InputCleanupError("Input release could not be confirmed. Restart Flippy before acting again.") from None

    @staticmethod
    def _gesture_mouse(kind, x, y, flags):
        ev = Quartz.CGEventCreateMouseEvent(None, kind, Quartz.CGPointMake(x, y), Quartz.kCGMouseButtonLeft)
        Quartz.CGEventSetFlags(ev, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)

    def _modifier(self, mod, flags):
        ev = Quartz.CGEventCreateKeyboardEvent(None, self.MOD_KEYS[mod], bool(flags & self.FLAGS[mod]))
        Quartz.CGEventSetFlags(ev, flags)
        Quartz.CGEventSetType(ev, Quartz.kCGEventFlagsChanged)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)

    def path(self, points, drag):
        """Compatibility wrapper for asynchronous scripted demo paths."""
        err = self._can_post()
        if err:
            return err
        if not points:
            return "empty path"
        threading.Thread(target=self._gesture, args=(points, drag), daemon=True).start()
        return "ok"

    def drag(self, x, y, to_x, to_y, modifiers=()):
        err = self._can_post()
        if err:
            return err
        points = [(x + (to_x - x) * i / 24, y + (to_y - y) * i / 24,
                   0.6 / 24 if i < 24 else 0) for i in range(25)]
        return self._gesture(points, True, modifiers)

    def scroll(self, x, y, direction, lines):
        err = self._can_post()
        if err:
            return err
        self._mouse(Quartz.kCGEventMouseMoved, x, y)
        time.sleep(0.025)
        vertical = lines if direction == "up" else -lines if direction == "down" else 0
        horizontal = lines if direction == "left" else -lines if direction == "right" else 0
        ev = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 2, vertical, horizontal)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        return "ok"

    def type_text(self, text, on_key=None, wait=False):
        """Real keystrokes, at a human-ish pace. on_key(ch) runs as each one is posted (on a worker thread)."""
        err = self._can_post()
        if err:
            return err

        def run():
            import random
            for ch in text:
                # The real key code where there is one (apps that read key codes, like GarageBand's
                # shortcuts, would otherwise see "a"), no stray modifiers, and the text for the field.
                from .keymap import type_key
                code, shifted = type_key(ch, hotkeys.KEYS)
                flags = Quartz.kCGEventFlagMaskShift if shifted else 0
                self._check_physical_input(code=code)
                try:
                    ev = Quartz.CGEventCreateKeyboardEvent(None, code, True)
                    Quartz.CGEventSetFlags(ev, flags)
                    Quartz.CGEventKeyboardSetUnicodeString(ev, len(ch.encode("utf-16-le")) // 2, ch)
                    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                finally:
                    try:
                        ev = Quartz.CGEventCreateKeyboardEvent(None, code, False)
                        Quartz.CGEventSetFlags(ev, flags)
                        Quartz.CGEventKeyboardSetUnicodeString(ev, len(ch.encode("utf-16-le")) // 2, ch)
                        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                    except Exception:
                        from ..actions import InputCleanupError
                        raise InputCleanupError("Input release could not be confirmed. Restart Flippy before acting again.") from None
                if on_key:
                    on_key(ch)
                time.sleep(random.uniform(0.045, 0.11) + (0.12 if ch in " ,." else 0))
            return "ok"
        if wait:
            return run()  # called on the agent's worker, never the Cocoa run loop
        threading.Thread(target=run, daemon=True).start()
        return "ok"

    # --- background tasks (flippy/mac/ax.py): one app's window and controls; your pointer and keyboard stay yours
    def app_preflight(self):
        from . import ax
        from ..actions import ActionError
        if not ax.trusted() or not Quartz.CGPreflightPostEventAccess():
            if not ax.ask_trust():
                subprocess.Popen(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"])
            raise ActionError("Flippy needs the Accessibility permission to do things for you. Turn Flippy on in "
                              "System Settings > Privacy & Security > Accessibility, then ask again. If it's already "
                              "on, remove it with - and add it again.")

    def app_front(self):
        from . import ax
        return ax.front_app()

    def app_open(self, name, cancel):
        from . import ax
        return ax.open_app(name, cancel)

    def app_look(self, bundle, name, pid):
        from . import ax
        return ax.look(bundle, name, pid)

    def app_act(self, name, args, frame, cancel):
        """press / set_text / focus / type / key / menu in the frame's app. Runs on the task's worker thread."""
        from . import ax
        from ..actions import ActionError, RetryableActionError
        pid = frame.target[1]
        if ax.running_app(pid) is None:
            raise ActionError("The app quit. Start a new /act request.")
        if cancel.is_set():
            raise ActionError("Task canceled.")
        el = frame.elements[args["element"]] if "element" in args else None
        if name == "press":
            ax.press(el[0], el[3])
        elif name == "set_text":
            ax.set_text(el[0], args["text"])
        elif name == "focus":
            ax.focus(el[0], pid)
        elif name == "type":
            ax.type_text(pid, args["text"], cancel)
        elif name == "key":
            ax.key(pid, args["combo"], self.FLAGS)
        elif name == "menu":
            ax.menu(pid, args["path"])
        elif name == "media":
            ax.media(args["action"])
        elif name == "app_action":
            from . import scripts
            app = scripts.app_for(args["action"])
            target = ax.running_app(pid)
            if app and (target is None or str(target.localizedName()) != app):
                raise RetryableActionError(f"{args['action']} works on {app}; use_app {app} first.")
            return scripts.run(args["action"], args["args"])
        elif name in ("click", "scroll", "drag") and args.get("real_pointer"):
            x, y = frame.to_logical(args["x"], args["y"])
            if name == "click":
                act = lambda: self.click(x, y, double=args["count"] == 2)  # noqa: E731
            elif name == "scroll":
                act = lambda: self.scroll(x, y, args["direction"], args["lines"])  # noqa: E731
            else:
                act = lambda: self.drag(x, y, *frame.to_logical(args["to_x"], args["to_y"]))  # noqa: E731
            self._borrow_pointer(pid, x, y, act, cancel)
        elif name in ("click", "scroll", "drag"):
            wid = frame.target[2]
            x, y = frame.to_logical(args["x"], args["y"])
            if name == "click":
                ax.click(pid, wid, x, y, args["count"], cancel)
            elif name == "scroll":
                ax.scroll(pid, wid, x, y, args["direction"], args["lines"])
            else:
                ax.drag(pid, wid, x, y, *frame.to_logical(args["to_x"], args["to_y"]), cancel)
        else:
            raise ActionError("Unsupported desktop action.")
        time.sleep(0.25)  # let the app redraw before the next look

    BORROW_IDLE_S = 1.0     # the pointer is borrowed only after this long without the user's input...
    BORROW_WAIT_S = 45.0    # ...waiting at most this long for such a pause

    def _borrow_pointer(self, pid, x, y, act, cancel):
        """For apps that ignore background clicks: wait until the user pauses, bring the app up, do the real
        click/scroll/drag, then put the pointer back and the app they were in back in front. A fraction of a second."""
        from . import ax
        from ..actions import ActionError, RetryableActionError
        hid = Quartz.kCGEventSourceStateHIDSystemState
        idle = lambda: Quartz.CGEventSourceSecondsSinceLastEventType(hid, Quartz.kCGAnyInputEventType)  # noqa: E731
        deadline = time.monotonic() + self.BORROW_WAIT_S
        while idle() < self.BORROW_IDLE_S:
            if cancel.is_set():
                raise ActionError("Task canceled.")
            if time.monotonic() > deadline:
                raise RetryableActionError("The user kept working, so Flippy didn't take the pointer. Try again later "
                                           "in the task, or use the app's controls, menus or keys.")
            time.sleep(0.1)
        target = ax.running_app(pid)
        if target is None:
            raise ActionError("The app quit. Start a new /act request.")
        before = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        home = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        try:
            target.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
            win = ax._window(pid)
            if win is not None:
                ax.AX.AXUIElementPerformAction(win, "AXRaise")
            # Up to 1.5 s for the app to come in front and its window to be the one at that spot: window order
            # lags activation, and checking once refused clicks that would have landed.
            for i in range(30):
                front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication().processIdentifier() == pid
                if front and self._window_owner_at(x, y) == pid:
                    break
                if i % 10 == 9 and win is not None:
                    ax.AX.AXUIElementPerformAction(win, "AXRaise")
                time.sleep(0.05)
            else:
                raise RetryableActionError("Another window still covered that spot after Flippy brought the app "
                                           "forward, so it didn't click. Look again and retry, or use a control, "
                                           "menu or key instead.")
            if idle() < 0.3:  # they picked the mouse back up while the app was coming forward
                raise RetryableActionError("The user started working again, so Flippy gave the pointer back. "
                                           "Try again later in the task.")
            result = act()
            if result not in (None, "ok"):
                raise ActionError(str(result))
            time.sleep(0.15)
        finally:
            Quartz.CGWarpMouseCursorPosition(home)            # the pointer back where it was
            Quartz.CGAssociateMouseAndMouseCursorPosition(True)
            if before is not None and before.processIdentifier() != pid and not ax._is_flippy(before):
                before.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)  # and their app

    @staticmethod
    def _window_owner_at(x, y):
        """pid of the frontmost normal window under that screen point."""
        info = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID)
        for win in info or []:
            b = win.get("kCGWindowBounds") or {}
            if win.get("kCGWindowLayer", 0) == 0 and b.get("X", 0) <= x < b.get("X", 0) + b.get("Width", 0) \
                    and b.get("Y", 0) <= y < b.get("Y", 0) + b.get("Height", 0):
                return win.get("kCGWindowOwnerPID")
        return None

    FLAGS = {"cmd": Quartz.kCGEventFlagMaskCommand, "shift": Quartz.kCGEventFlagMaskShift,
             "option": Quartz.kCGEventFlagMaskAlternate, "ctrl": Quartz.kCGEventFlagMaskControl}
    MOD_KEYS = {"cmd": 55, "shift": 56, "option": 58, "ctrl": 59}

    HELD_GRACE_S = 0.4  # a key or click that just ended (yours, or Flippy's own last input) can still read as down

    def _check_physical_input(self, code=None, mouse=False):
        """Never pair synthetic release with input the user already holds. Waits briefly for input that is
        just ending, so the click on "Allow" or Flippy's previous keystroke doesn't count as held."""
        state = Quartz.kCGEventSourceStateCombinedSessionState
        codes = set(self.MOD_KEYS.values())
        if code is not None:
            codes.add(code)
        deadline = time.monotonic() + self.HELD_GRACE_S
        while True:
            held_mouse = bool(mouse and Quartz.CGEventSourceButtonState(state, Quartz.kCGMouseButtonLeft))
            held_keys = sorted(k for k in codes if Quartz.CGEventSourceKeyState(state, k))
            if not held_mouse and not held_keys:
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        print(f"flippy: act: input still held after {self.HELD_GRACE_S}s (mouse={held_mouse}, key codes {held_keys})",
              flush=True)
        from ..actions import InputHeldError
        raise InputHeldError("Let go of the keyboard and mouse while Flippy acts.")

    def app_name(self, bundle_id, pid):
        """The app's display name for approval cards ("Notes"), or None."""
        from . import ax
        app = ax.running_app(pid) if type(pid) is int else None
        return str(app.localizedName()) if app is not None and app.localizedName() else None

    def key(self, combo):
        """A shortcut like cmd+shift+space, escape or return, pressed for real."""
        err = self._can_post()
        if err:
            return err
        parts = [p.strip().lower() for p in combo.split("+")]
        try:
            code = hotkeys.KEYS[parts[-1]]
        except KeyError:
            return f"unknown key {parts[-1]!r}"
        flags = 0
        for m in parts[:-1]:
            flags |= self.FLAGS.get({"opt": "option", "alt": "option", "control": "ctrl"}.get(m, m), 0)
        self._check_physical_input(code=code)
        try:
            ev = Quartz.CGEventCreateKeyboardEvent(None, code, True)
            Quartz.CGEventSetFlags(ev, flags)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.03)
        finally:
            try:
                ev = Quartz.CGEventCreateKeyboardEvent(None, code, False)
                Quartz.CGEventSetFlags(ev, flags)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            except Exception:
                from ..actions import InputCleanupError
                raise InputCleanupError("Input release could not be confirmed. Restart Flippy before acting again.") from None
        return "ok"

    def tap(self, mod, times):
        """Tap a modifier on its own (for double-tap shortcuts)."""
        err = self._can_post()
        if err:
            return err
        code, flag = self.MOD_KEYS[mod], self.FLAGS[mod]

        def run():
            for _ in range(times):
                for down in (True, False):
                    ev = Quartz.CGEventCreateKeyboardEvent(None, code, down)
                    Quartz.CGEventSetFlags(ev, flag if down else 0)
                    Quartz.CGEventSetType(ev, Quartz.kCGEventFlagsChanged)
                    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                    time.sleep(0.07 if down else 0.12)
        threading.Thread(target=run, daemon=True).start()
        return "ok"

    def press_nudge(self, title):
        return self.nudge.press(title)

    def restart(self, full_install=False):
        """Start a fresh Flippy and quit this one. full_install: rerun ./install.sh first (the app launcher changed)."""
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        app = os.environ.get("FLIPPY_APP")
        from ..profile import current
        if full_install and current().demo:
            return "Rebuild Flippy Demo with scripts/dev.sh build."
        command = ([root + "/install.sh"] if full_install else
                   ["/usr/bin/open", "-n", app] if app else [root + "/bin/flippy-ask", "start"])
        # Wait for this daemon's cleanup, rather than reopening a still-running instance.
        runner = '''import socket, subprocess, sys, time
deadline = time.monotonic() + 30
while True:
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(1)
            s.connect(sys.argv[1])
    except (FileNotFoundError, ConnectionRefusedError):
        break
    if time.monotonic() >= deadline:
        raise SystemExit("restart stopped: previous instance is still cleaning up")
    time.sleep(.1)
subprocess.run(sys.argv[2:], check=False)
'''
        with open(current().log, "ab") as output:
            subprocess.Popen([sys.executable, "-c", runner, current().socket, *command], stdout=output, stderr=output,
                             start_new_session=True)
        self.flippy.quit()

    def permission_state(self):
        return {"screen_recording": bool(CGPreflightScreenCaptureAccess()),
                "accessibility": bool(hotkeys.accessibility_trusted())}

    def show_update(self, rel, install, later):
        notes = next((ln.strip("-*# ").strip() for ln in rel["notes"].splitlines() if ln.strip("-*# ").strip()),
                     "A new version is ready.")
        self.nudge.card(f"Flippy {rel['version']} is out", notes,
                        [("What's new", lambda: subprocess.Popen(["open", rel["url"]])), ("Later", later),
                         ("Install", install)])

    def quit(self):
        self.stop_recording()
        AppHelper.callAfter(NSApp.terminate_, None)

    def focused(self):
        """As the question box opens (Electron apps stop reporting their focus once it takes the keyboard): the
        focused element and window of the app you're in, for work_area. A few quick lookups, so the box isn't held up."""
        from . import ax
        try:
            ax.prime_focused_area()
            return ax.focused()
        except Exception as e:
            print(f"flippy: focused: {type(e).__name__}", flush=True)
            return None

    def work_area(self, focus=None):
        """(the text you were working in, its window), each (x, y, w, h) or None: a docked answer stays off them
        (flippy/placement.py). focus: what focused() took as the box opened; else, or if it gives nothing, the app's
        focus now. Lookups to another app: call it off the main thread."""
        from . import ax
        try:
            def frames(el, window):
                return ax.text_area(el), ax.window_frame(window)
            found = frames(*(focus or (None, None)))
            return found if any(found) else frames(*ax.focused())
        except Exception as e:  # never in the way of an answer
            print(f"flippy: work_area: {type(e).__name__}", flush=True)
            return None, None

    def screen_size(self):
        f = main_screen().frame()
        return f.size.width, f.size.height

    def scale(self):
        return main_screen().backingScaleFactor()


def _already_running(sock_path):
    """Opening Flippy.app again while it runs: show the settings there instead of starting twice."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(sock_path)
            s.sendall(b"settings\n")
            s.recv(64)
        return True
    except OSError:
        return False


def run(make_app):
    """Start Cocoa and call make_app(platform) once it's up."""
    from ..daemon import SOCK_PATH
    if _already_running(SOCK_PATH):
        print("flippy: already running; opened its settings", flush=True)
        return
    # the daemon is a Python child of Flippy.app, so its menu title (and Dock name, while the app window is open)
    # would read "Python"
    info = AppKit.NSBundle.mainBundle().infoDictionary()
    if info is not None:
        info["CFBundleName"] = "Flippy"
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # no Dock icon
    install_edit_keys()
    global PLATFORM
    platform = PLATFORM = Platform()
    flippy = make_app(platform)
    platform.flippy = flippy
    platform.start(flippy.command)
    AppHelper.runEventLoop(installInterrupt=False)  # controller owns graceful SIGINT cleanup
