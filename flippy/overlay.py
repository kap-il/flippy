"""The overlay's state and painting, shared by both platforms.

The overlay is a permanent full-screen, click-through surface holding the
pointer, the answer card and the user's marks. Everything is painted with
cairo by the active theme (flippy/themes.py). A platform subclass
(flippy/linux/ui.py, flippy/mac/ui.py) owns the actual window and implements
the hooks at the bottom: redraw, opacity, ticking, and which parts take clicks.
It feeds mouse input in through press/motion/release/right_click.
"""
import time

import cairo

from . import placement, settings, themes

DOCK_TOP = 40               # a docked answer stays below the menu bar...
PLAN_H = 180                # ...and its spot is picked for at least this height: answers grow as they play


class OverlayBase:
    card_margin_top = 70
    card_margin_bottom = 110  # clear the dock
    has_backdrop = False      # the platform can put real blurred glass behind the card (Theme.backdrop)

    def __init__(self):
        self.card = None          # themes.Card being shown, or None
        self.card_mode = "top"    # "top" | "bottom" (centered) | "follow" (next to the pointer) | "dock" (placement)
        self.card_t0 = 0.0
        self.meta = ""            # shown by themes that have room for it (e.g. "OPUS · LOW")
        self.target = None        # (x, y, label) in logical px: where the pointer is heading
        self.pos = None           # where the pointer is drawn right now (animated)
        self.move_from = None
        self.move_t0 = 0.0
        self.t0 = 0.0
        self.ticking = False
        self.pinned = False       # the answer stays until dismissed (the card's pushpin)
        # Docking (look.card_place): an answer to someone learning something on screen goes where it blocks the
        # least (flippy/placement.py) and stays there; the pointer moves on its own, drawn over it.
        self.learning = False     # the reply said so ([LEARN], flippy/point.py)
        self.keep = None          # (x, y, w, h): what Claude said they need to keep seeing
        self.spot = None          # (x or None, right or None, y or None, bottom or None): the edges it's held by
        self.avoid = None         # (x, y, w, h): the text you were working in when you asked (platform work_area)
        self.window = None        # ...and its window
        self.detail = None        # placement.Detail of the screenshot the question was asked about

        # draw mode
        self.strokes: list[list[tuple[float, float]]] = []
        self.drawing = False
        self.on_draw_done = lambda: None
        self.on_draw_cancel = lambda: None

        # player controls (Media Player): only these rects take clicks; the rest stays click-through
        self.hits = {}            # {name: (x, y, w, h)} from the theme, refreshed every frame
        self.thought_open = True  # a task card's thinking section: open, or folded to one line
        self.on_control = lambda name, frac: None
        self.pressed = (None, 0.0)
        self.slider = None        # (name, press x) while dragging the seek/speed slider

    @property
    def theme(self):
        return themes.get(settings.get("look", "theme"))

    @property
    def showing(self):
        return self.card is not None or self.target is not None or bool(self.strokes)

    def show_text(self, text, error=False, at_bottom=False, header=None, follow=False, **card_fields):
        if self.card is None:
            self.card_t0 = time.monotonic()
        self.card = themes.Card(text=text, error=error, header=header, follow=bool(follow and self.target),
                                meta=self.meta, pinned=self.pinned, **card_fields)
        if self.pinned and not themes.pin_shown(self.card):  # a notice after a pinned answer: no pin to undo, so it fades
            self.pinned = self.card.pinned = False
        place = settings.get("look", "card_place")  # auto: docks when they're learning something on screen
        if themes.pin_shown(self.card) and (place == "corner" or place == "auto" and self.learning):
            self.card_mode = "dock"
        elif self.card.follow:
            self.card_mode = "follow"
        else:
            self.card_mode = "bottom" if at_bottom else "top"
        self.set_opacity(1.0)
        self._ensure_tick()

    def point(self, x, y, label):
        """Move the pointer to (x, y): glide there from where it is, or, when it isn't showing yet, out from the
        mouse, where the question box opened (#37); drop in where the platform can't say where the mouse is."""
        now = time.monotonic()
        if self.pos is None:
            start = self.mouse()
            self.pos = start or (x, y)
            self.t0 = now
            if start:
                self.move_from, self.move_t0 = start, now
        else:
            self.move_from = self.pos
            self.move_t0 = now
        self.target = (x, y, label)
        self._ensure_tick()

    def clear(self, keep_marks=False):
        self.card = None
        self.pinned = self.learning = False
        self.keep = self.spot = None
        self.avoid = self.window = self.detail = None
        self.target = None
        self.pos = None
        self.move_from = None
        if not keep_marks:
            self.strokes = []
        if self.ticking and not self.strokes:
            self.stop_ticking()
            self.ticking = False
        self.queue_draw()
        self.set_opacity(1.0)

    def _ensure_tick(self):
        if not self.ticking:
            self.ticking = True
            self.start_ticking()

    def tick(self):
        """Once per frame while something is showing."""
        if self.target and self.move_from:
            glide = settings.get("timing", "glide_seconds") / settings.get("timing", "speed")
            k = min((time.monotonic() - self.move_t0) / glide, 1.0)
            k = k * k * (3 - 2 * k)  # smoothstep
            (fx, fy), (tx, ty) = self.move_from, self.target[:2]
            self.pos = (fx + (tx - fx) * k, fy + (ty - fy) * k)
            if k >= 1.0:
                self.move_from = None
        self.queue_draw()

    def pointer_style(self):
        style = settings.get("look", "pointer")
        return self.theme.pointer if style == "theme" else style

    def _card_rect(self, W, H, opts):
        cw, ch = self.theme.card_size(self.card, opts)
        if self.card_mode == "follow" and self.pos:
            x, y = self.pos
            right, left, below, above = themes.pointer_extent(self.pointer_style(), settings.get("look", "pointer_size"))
            flip = y + below + 4 > H
            cx = x + right + 14                     # right of the pointer...
            if cx + cw > W - 8:
                cx = x - left - 14 - cw             # ...or left of it near the edge
            cy = (y - ch - 16) if flip else (y + 16)
            cx = min(max(cx, 8), W - cw - 8)
            cy = min(max(cy, 8), H - ch - 8)
        elif self.card_mode == "dock":
            return self._dock_rect(W, H, cw, ch)
        else:
            cx = (W - cw) / 2
            cy = H - ch - self.card_margin_bottom if self.card_mode == "bottom" else self.card_margin_top
        return cx, cy, cw, ch

    def _dock_rect(self, W, H, cw, ch):
        """Where the docked answer is: its spot is picked once, then held by the edges nearest the screen's, so the
        card grows away from them as the answer plays."""
        if self.spot is None:
            plan = max(ch, PLAN_H)
            x, y = placement.best_spot(W, H, cw, plan, keep=self.keep, work=self.avoid, window=self.window,
                                       detail=self.detail, top=DOCK_TOP, bottom=self.card_margin_bottom)
            right = x + cw / 2 > W / 2
            bottom = y + plan / 2 > H / 2
            self.spot = (None if right else x, x + cw if right else None, None if bottom else y, y + plan if bottom else None)
        x, right, y, bottom = self.spot
        cx = right - cw if x is None else x
        cy = bottom - ch if y is None else y
        return min(max(cx, 8), W - cw - 8), min(max(cy, 8), H - ch - 8), cw, ch

    # --- draw mode ---
    def start_drawing(self):
        self.drawing = True
        self.strokes = []
        self.set_drawing_input(True)
        self.set_opacity(1.0)
        self._ensure_tick()

    def stop_drawing(self):
        self.drawing = False
        self.set_drawing_input(False)

    def _hit(self, x, y):
        for name, (rx, ry, rw, rh) in self.hits.items():
            if rx <= x <= rx + rw and ry <= y <= ry + rh:
                return name, (x - rx) / rw if rw else 0
        return None, 0

    # --- mouse input, in logical px (fed by the platform window) ---
    def press(self, x, y):
        if self.drawing:
            self.strokes.append([(x, y)])
            return
        name, frac = self._hit(x, y)
        if name:
            self.pressed = (name, time.monotonic())
            if name in ("seek", "speed"):
                self.slider = (name, x)
            self.on_control(name, frac)
            self.queue_draw()

    def motion(self, x, y):
        if self.drawing and self.strokes:
            self.strokes[-1].append((x, y))
        elif self.slider and self.slider[0] in self.hits:
            name = self.slider[0]
            rx, _, rw, _ = self.hits[name]
            self.pressed = (name, time.monotonic())
            self.on_control(name, (x - rx) / rw)

    def release(self):
        self.slider = None
        if not self.drawing or not self.strokes:
            return
        if len(self.strokes[-1]) < 4:  # a click, not a mark; keep drawing
            self.strokes.pop()
            return
        self.on_draw_done()

    def right_click(self):
        if self.drawing:
            self.on_draw_cancel()

    # --- painting ---
    def _draw_strokes(self, cr):
        r, g, b = self.theme.pen
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        for width, alpha in ((12, 0.25), (5, 1.0)):  # soft glow, then the solid line
            cr.set_line_width(width)
            cr.set_source_rgba(r, g, b, alpha)
            for stroke in self.strokes:
                if not stroke:
                    continue
                cr.move_to(*stroke[0])
                for pt in stroke[1:]:
                    cr.line_to(*pt)
                cr.stroke()

    def paint(self, cr, w, h):
        """Paint everything into cr (logical px, w x h screen) and refresh the clickable regions."""
        theme = self.theme
        now = time.monotonic()
        lay = self.card_layout(w, h)
        if self.strokes:
            cr.save()
            if lay:  # the card sits over the marks: a stroke showing through its glass makes the text hard to read
                x, y, cw, ch = lay[0]
                cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
                cr.rectangle(0, 0, w, h)
                themes.round_rect(cr, x, y, cw, ch, (theme.backdrop or {}).get("radius", 20))
                cr.clip()
            self._draw_strokes(cr)
            cr.restore()
        hits = {}
        if lay:
            (x, y, cw, ch), opts = lay
            theme.draw(cr, x, y, cw, ch, self.card, now - self.card_t0, opts)
            hits = theme.hit_regions(self.card, x, y, cw, ch, opts)
            if themes.pin_shown(self.card):
                theme.draw_pin(cr, self.card, x, y, cw, ch, opts)
                hits["pin"] = theme.pin_rect(self.card, x, y, cw, ch, opts)
            if self.card.task and self.card.thought:  # a desktop task's thinking section, inside its card
                hits["thought"] = theme.draw_thought(cr, self.card, x, y, cw, ch, opts)
            if self.card.actions:  # a walkthrough's "Follow up", a side answer's way back to it
                hits.update(theme.draw_actions(cr, self.card, x, y, cw, ch, opts))
        if self.target and self.pos:  # over the card: a docked card never hides what it's pointing at
            themes.draw_pointer(cr, theme, self.pointer_style(), *self.pos, now - self.t0,
                                settings.get("look", "pointer_size"), h, backdrop=self.has_backdrop)
        self.hits = hits
        self.hits_changed()

    def pointer_lens(self):
        """(cx, cy, r) of the Liquid Glass pointer when it's showing and the platform has real glass, else None."""
        if not (self.has_backdrop and self.target and self.pos and self.pointer_style() == "glass"):
            return None
        return themes.glass_lens(*self.pos, time.monotonic() - self.t0, settings.get("look", "pointer_size"))

    def pointer_hand(self, screen_h):
        """The Liquid Glass hand's pieces (themes.glass_hand) when it's showing on real glass, else None."""
        if not (self.has_backdrop and self.target and self.pos and self.pointer_style() == "glasshand"):
            return None
        return themes.glass_hand(*self.pos, time.monotonic() - self.t0, settings.get("look", "pointer_size"), screen_h)

    def card_layout(self, w, h):
        """((x, y, w, h), theme opts) of the card on a w x h screen, or None when there's no card."""
        if not self.card:
            return None
        name, at = self.pressed
        opts = {"text_size": settings.get("look", "text_size"), "card_opacity": settings.get("look", "card_opacity"),
                "controls": settings.get("look", "controls"),
                "pressed": name if (time.monotonic() - at < 0.18 or self.slider) else None,
                "backdrop": self.has_backdrop and bool(self.theme.backdrop),
                "player_shine": settings.get("look", "player_shine"), "thought_open": self.thought_open,
                "card_w": self.card_width()}
        return self._card_rect(w, h, opts), opts

    def card_width(self):
        """The card's width in px, for every theme (macOS: the question box's), or None for each theme's own."""
        return None

    def mouse(self):
        """Where the mouse is in overlay coordinates, or None where the platform can't say."""
        return None

    # --- platform hooks ---
    def queue_draw(self):
        raise NotImplementedError

    def set_opacity(self, a):
        raise NotImplementedError

    def start_ticking(self):
        """Call self.tick() every frame until stop_ticking()."""
        raise NotImplementedError

    def stop_ticking(self):
        raise NotImplementedError

    def set_drawing_input(self, on):
        """Draw mode on: the whole screen takes the mouse (crosshair). Off: back to click-through."""
        raise NotImplementedError

    def hits_changed(self):
        """self.hits was refreshed: only those rects should take clicks (outside draw mode)."""
