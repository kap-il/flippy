"""Themes: how the answer card, pointer, drawing pen and question box look.

A theme paints the card itself with cairo/Pango (so skins like the Y2K player can
do bevels, LCDs and visualizers), picks a default pointer and pen color, and
supplies CSS for the question box. Add a theme by subclassing Theme and adding
it to THEMES.
"""
import ctypes
import math
import os
import re
import sys
from dataclasses import dataclass, field

import cairo

if sys.platform == "darwin":
    from .mac import text as mactext
else:
    import gi

    gi.require_version("Pango", "1.0")
    gi.require_version("PangoCairo", "1.0")
    from gi.repository import Pango, PangoCairo  # noqa: E402

from . import pointers  # noqa: E402

FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
PIXEL_FONT = "VT323"
PROMPT = "flippy@mac:~" if sys.platform == "darwin" else "flippy@cosmic:~"  # Terminal theme title bar


def load_fonts():
    """Register bundled fonts for this process only (nothing installed system-wide)."""
    if sys.platform == "darwin":
        mactext.load_fonts(FONT_DIR)
        return
    try:
        fc = ctypes.CDLL("libfontconfig.so.1")
        for f in os.listdir(FONT_DIR):
            if f.endswith((".ttf", ".otf")):
                fc.FcConfigAppFontAddFile(None, os.path.join(FONT_DIR, f).encode())
    except OSError:
        pass


# ---------------------------------------------------------------- card state

@dataclass
class Card:
    text: str = ""
    header: str | None = None     # label of the thing being pointed at
    error: bool = False
    phase: str = "playing"        # thinking | playing | done | info
    steps: list = field(default_factory=list)  # walkthrough labels so far
    step: int = 0                 # current step index
    progress: float = 0.0         # 0..1 through the answer
    typing: bool = False          # text still typing in
    meta: str = ""                # e.g. "OPUS · LOW"
    follow: bool = False          # riding next to the pointer (narrower)
    paused: bool = False          # walkthrough paused by the user
    finished: bool = False        # the whole answer has played
    speed: float = 1.0            # playback speed (shown on player skins)
    controls: bool = False        # a real answer: show playback controls on themes that hide them otherwise


SPEED_MIN, SPEED_MAX = 0.5, 2.0


def speed_to_frac(speed):
    return (speed - SPEED_MIN) / (SPEED_MAX - SPEED_MIN)


def frac_to_speed(frac):
    return round((SPEED_MIN + max(0.0, min(frac, 1.0)) * (SPEED_MAX - SPEED_MIN)) * 20) / 20  # 0.05 steps


def lean_controls(card, opts):
    """Controls on the non-player themes: answers only, and only if enabled in settings."""
    return (card.controls and opts.get("controls", "all") == "all"
            and card.phase != "thinking" and not card.error)


def glyph(cr, name, cx, cy, s, rgba):
    """Media glyphs centered at (cx, cy), size s (~half-height)."""
    cr.set_source_rgba(*rgba)
    if name == "play":
        cr.move_to(cx - s * 0.6, cy - s)
        cr.line_to(cx - s * 0.6, cy + s)
        cr.line_to(cx + s * 0.9, cy)
        cr.close_path()
    elif name == "pause":
        cr.rectangle(cx - s * 0.75, cy - s, s * 0.55, s * 2)
        cr.rectangle(cx + s * 0.2, cy - s, s * 0.55, s * 2)
    elif name in ("prev", "next"):
        d = 1 if name == "next" else -1
        cr.move_to(cx - d * s * 0.8, cy - s * 0.9)
        cr.line_to(cx - d * s * 0.8, cy + s * 0.9)
        cr.line_to(cx + d * s * 0.5, cy)
        cr.close_path()
        cr.fill()
        cr.rectangle(cx + d * s * 0.5 - (s * 0.3 if d > 0 else 0), cy - s * 0.9, s * 0.3, s * 1.8)
    elif name == "close":
        cr.set_line_width(max(1.5, s * 0.3))
        cr.move_to(cx - s * 0.7, cy - s * 0.7)
        cr.line_to(cx + s * 0.7, cy + s * 0.7)
        cr.move_to(cx + s * 0.7, cy - s * 0.7)
        cr.line_to(cx - s * 0.7, cy + s * 0.7)
        cr.stroke()
        return
    cr.fill()


def thinking(card):
    """Waiting for the model's first words."""
    return card.phase == "thinking" and not card.error


def thinking_word(t, word="thinking"):
    """ "thinking", "thinking.", "thinking..", "thinking..." on a loop, so the card visibly works."""
    return word + "." * (int(t * 3) % 4)


def sweep(cr, x, y, w, h, t, rgba, radius=0):
    """An indeterminate progress bar: a lit segment gliding along the track, wrapping at the end."""
    seg = w * 0.32
    pos = ((t / 1.4) % 1.0) * (w + seg) - seg
    cr.save()
    cr.rectangle(x, y, w, h)
    cr.clip()
    if radius:
        round_rect(cr, x + pos, y, seg, h, radius)
    else:
        cr.rectangle(x + pos, y, seg, h)
    cr.set_source_rgba(*rgba)
    cr.fill()
    cr.restore()


def playing(card):
    """Should a player skin show 'pause' (i.e. something is in progress)?"""
    return card.phase == "thinking" or not (card.paused or card.finished)


# ---------------------------------------------------------------- helpers

_measure_ctx = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1))


def layout(cr, text, family, px, width=None, bold=False, mono=False):
    if sys.platform == "darwin":
        return mactext.Layout(cr, text, family, px, width, bold)
    lay = PangoCairo.create_layout(cr)
    fd = Pango.FontDescription.from_string(family)
    fd.set_absolute_size(px * Pango.SCALE)
    if bold:
        fd.set_weight(Pango.Weight.BOLD)
    lay.set_font_description(fd)
    if width:
        lay.set_width(int(width * Pango.SCALE))
        lay.set_wrap(Pango.WrapMode.WORD_CHAR)
    lay.set_text(text, -1)
    return lay


def lsize(lay):
    if sys.platform == "darwin":
        return lay.size()
    _, logical = lay.get_pixel_extents()
    return logical.width, logical.height


def show(cr, lay, x, y, rgba):
    cr.set_source_rgba(*rgba)
    if sys.platform == "darwin":
        lay.show(cr, x, y)
        return
    cr.move_to(x, y)
    PangoCairo.show_layout(cr, lay)


def ellipsize_end(lay):
    lay.set_ellipsize(True if sys.platform == "darwin" else Pango.EllipsizeMode.END)


def round_rect(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def hexrgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


# ---------------------------------------------------------------- pointers

HAND = [
    "     ##       ",
    "    #..#      ",
    "    #..#      ",
    "    #..#      ",
    "    #..#      ",
    "    #..#      ",
    "    #..#      ",
    "  ###..###    ",
    " #..#..#..##  ",
    " #..#..#..#.# ",
    "##..........# ",
    "#.#.........# ",
    "#..#........# ",
    " #..........# ",
    "  #.........# ",
    "  #........#  ",
    "   #.......#  ",
    "   #.......#  ",
    "   #########  ",
]
HAND_TIP_COL = 6
ARROW = [
    "#          ",
    "##         ",
    "#.#        ",
    "#..#       ",
    "#...#      ",
    "#....#     ",
    "#.....#    ",
    "#......#   ",
    "#.......#  ",
    "#........# ",
    "#.....#####",
    "#..#..#    ",
    "#.# #..#   ",
    "##  #..#   ",
    "#    #..#  ",
    "     #..#  ",
    "      ##   ",
]


def _sprite(cr, grid, ox, oy, px, fill, outline, flip=False, shadow=False):
    for r, row in enumerate(grid[::-1] if flip else grid):
        for c, ch in enumerate(row):
            if ch == " ":
                continue
            if shadow:
                cr.set_source_rgba(0, 0, 0, 0.35)
            else:
                cr.set_source_rgb(*(outline if ch == "#" else fill))
            cr.rectangle(ox + c * px, oy + r * px, px, px)
            cr.fill()


def sprite_px(size):
    return max(2, round(4 * size))


def pointer_extent(style, size):
    """(right, left, below, above) space the pointer takes around its target, in px."""
    px = sprite_px(size)
    if style.startswith(pointers.PREFIX):
        return pointers.extent(style[len(pointers.PREFIX):], size, px)
    if style == "hand":
        return (len(HAND[0]) - HAND_TIP_COL) * px, HAND_TIP_COL * px, len(HAND) * px, len(HAND) * px
    if style == "arrow":
        return len(ARROW[0]) * px, 0, len(ARROW) * px, 0
    if style == "glass":
        r = 26 * size
        return r, r, r, r
    if style == "glasshand":
        return 20 * size, 26 * size, 50 * size, 50 * size
    r = 26 * size
    return r, r, r, r


def glass_lens(x, y, t, size):
    """The Liquid Glass pointer: a lens centered on the target. (cx, cy, radius), animated: it drops in and breathes."""
    intro = min(t / 0.3, 1.0)
    drop = -40 * (1 - intro) ** 3
    breathe = 1 + 0.05 * math.sin(t * 4) if intro >= 1 else 1
    return x, y + drop, 22 * size * breathe


def _draw_lens(cr, theme, x, y, t, size, backdrop):
    """Light on the lens: shadow, rim, specular arcs, and a precise dot at the tip.
    With a backdrop (macOS) the real glass sits underneath; without, a faint fill stands in for it."""
    cx, cy, r = glass_lens(x, y, t, size)
    for k, a in ((1.35, 0.06), (1.2, 0.1), (1.08, 0.14)):  # soft shadow under the lens
        cr.arc(cx + 1, cy + 3, r * k, 0, 2 * math.pi)
        cr.set_source_rgba(0, 0, 0, a)
        cr.fill()
    if not backdrop:
        g = cairo.RadialGradient(cx - r * 0.3, cy - r * 0.4, 0, cx, cy, r)
        g.add_color_stop_rgba(0, 1, 1, 1, 0.35)
        g.add_color_stop_rgba(1, 0.6, 0.7, 0.8, 0.18)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.set_source(g)
        cr.fill()
    cr.arc(cx, cy, r - 0.75, 0, 2 * math.pi)  # rim: bright on top, dimmer below
    g = cairo.LinearGradient(0, cy - r, 0, cy + r)
    g.add_color_stop_rgba(0, 1, 1, 1, 0.9)
    g.add_color_stop_rgba(0.55, 1, 1, 1, 0.22)
    g.add_color_stop_rgba(1, 1, 1, 1, 0.5)
    cr.set_source(g)
    cr.set_line_width(1.5)
    cr.stroke()
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.arc(cx, cy, r * 0.78, math.radians(200), math.radians(255))  # specular glint, top left
    cr.set_source_rgba(1, 1, 1, 0.75)
    cr.set_line_width(2.2 * size)
    cr.stroke()
    cr.arc(cx, cy, r * 0.8, math.radians(20), math.radians(70))  # refraction glow, bottom right
    cr.set_source_rgba(1, 1, 1, 0.28)
    cr.set_line_width(1.6 * size)
    cr.stroke()
    cr.set_line_cap(cairo.LINE_CAP_BUTT)
    cr.arc(x, cy, 3.2 * size, 0, 2 * math.pi)  # a precise dot at the exact spot
    cr.set_source_rgba(0, 0, 0, 0.45)
    cr.fill()
    cr.arc(x, cy, 2.2 * size, 0, 2 * math.pi)
    cr.set_source_rgba(1, 1, 1, 0.95)
    cr.fill()


# The Liquid Glass hand: rounded pieces that a glass container fuses into one shape. It points the way
# the app icon's pixel hand always looked like it was pointing: middle finger up, the rest curled.
# (x, y, w, h, corner radius, rotation in degrees), in px at size 1, with the fingertip at (0, 0), pointing up.
# The first piece must be the pointing finger (the tip dot sits in it).
GLASS_HAND = [
    (-5, 0, 10, 36, 5, 0),        # middle finger, up and long
    (-15, 25, 34, 24, 10, 0),     # the fist
    (-15, 19, 10, 13, 5, 0),      # curled index
    (5, 19, 9, 13, 4.5, 0),       # curled ring
    (13, 22, 7, 11, 3.5, 0),      # curled pinky
    (-25, 26, 18, 10, 5, 35),     # thumb: one piece, angled up and out, its inner end buried in the fist
]


def glass_hand(x, y, t, size, screen_h):
    """Where the glass hand's pieces go: [(x, y, w, h, radius, rotation)] in screen px, tip on (x, y).
    Drops in and bobs like the pixel hand, and flips to point down when there's no room below."""
    intro = min(t / 0.3, 1.0)
    drop = -40 * (1 - intro) ** 3
    bob = 3 * math.sin(t * 4) if intro >= 1 else 0
    flip = y + 50 * size + 4 > screen_h
    out = []
    for px, py, w, h, r, rot in GLASS_HAND:
        if flip:  # mirror top-bottom around the tip
            py, rot = -(py + h), -rot
        oy = (-2 - drop - bob) if flip else (2 + drop + bob)
        out.append((x + px * size, y + oy + py * size, w * size, h * size, r * size, rot))
    return out


def _piece_path(cr, px, py, w, h, r, rot):
    cr.save()
    cr.translate(px + w / 2, py + h / 2)
    cr.rotate(math.radians(rot))
    round_rect(cr, -w / 2, -h / 2, w, h, r)
    cr.restore()


def _union(cr, pieces, paint):
    """Fill the pieces as one shape (no darker overlaps), then paint(cr) it from a group."""
    cr.push_group()
    for pc in pieces:
        _piece_path(cr, *pc)
    cr.set_source_rgba(0, 0, 0, 1)
    cr.fill()
    cr.pop_group_to_source()
    paint(cr)


def _draw_glass_hand(cr, theme, x, y, t, size, screen_h, backdrop):
    pieces = glass_hand(x, y, t, size, screen_h)
    cr.save()  # shadow
    cr.translate(2, 3)
    _union(cr, pieces, lambda c: c.paint_with_alpha(0.22))
    cr.restore()
    if not backdrop:  # no real glass: a milky body stands in
        cr.push_group()
        _union(cr, pieces, lambda c: c.paint())
        cr.set_operator(cairo.OPERATOR_IN)
        cr.set_source_rgba(1, 1, 1, 0.3)
        cr.paint()
        cr.pop_group_to_source()
        cr.paint()
    # rim: stroke every piece, then clear their insides, leaving the outline of the whole hand
    top, bot = min(p[1] for p in pieces), max(p[1] + p[3] for p in pieces)
    cr.push_group()
    for pc in pieces:
        _piece_path(cr, *pc)
    cr.set_line_width(3)
    g = cairo.LinearGradient(0, top, 0, bot)
    g.add_color_stop_rgba(0, 1, 1, 1, 0.95)
    g.add_color_stop_rgba(0.5, 1, 1, 1, 0.35)
    g.add_color_stop_rgba(1, 1, 1, 1, 0.6)
    cr.set_source(g)
    cr.stroke()
    cr.set_operator(cairo.OPERATOR_CLEAR)
    for pc in pieces:
        _piece_path(cr, *pc)
    cr.fill()
    cr.pop_group_to_source()
    cr.paint()
    fx, fy, fw, fh = pieces[0][:4]  # the exact spot: just inside the fingertip, whichever way it points
    tip = fy + 4 * size if abs(fy - y) < abs(fy + fh - y) else fy + fh - 4 * size
    cr.arc(x, tip, 2.2 * size, 0, 2 * math.pi)
    cr.set_source_rgba(1, 1, 1, 0.95)
    cr.fill()


def draw_pointer(cr, theme, style, x, y, t, size, screen_h, backdrop=False):
    """Pointer with its hot spot at (x, y). t = seconds since it appeared.
    backdrop: the platform puts real glass under the "glass" pointer (see glass_lens)."""
    intro = min(t / 0.3, 1.0)
    drop = -40 * (1 - intro) ** 3
    bob = 3 * math.sin(t * 4) if intro >= 1 else 0
    pulse = 0.5 + 0.5 * math.sin(t * 4)
    tr, tg, tb = theme.tap
    px = sprite_px(size)
    if style.startswith(pointers.PREFIX):
        if pointers.draw(cr, style[len(pointers.PREFIX):], x, y, t, size, px, screen_h, theme.tap):
            return
        style = theme.pointer  # missing/broken custom pointer: fall back to the theme's
    if style == "glass":
        _draw_lens(cr, theme, x, y, t, size, backdrop)
        return
    if style == "glasshand":
        _draw_glass_hand(cr, theme, x, y, t, size, screen_h, backdrop)
        return
    if style in ("hand", "arrow"):
        cr.set_source_rgba(tr, tg, tb, 0.25 + 0.2 * pulse)  # tap ring at the exact spot
        cr.arc(x, y, (10 + 4 * pulse) * size, 0, 2 * math.pi)
        cr.fill()
    if style == "hand":
        h = len(HAND) * px
        flip = y + h + 4 > screen_h  # no room below: point down at it instead
        hx = x - HAND_TIP_COL * px
        hy = (y - h - 2 - drop - bob) if flip else (y + 2 + drop + bob)
        _sprite(cr, HAND, hx + 2, hy + 3, px, None, None, flip, shadow=True)
        _sprite(cr, HAND, hx, hy, px, theme.hand_fill, theme.hand_outline, flip)
    elif style == "arrow":
        ay = y + drop + bob
        _sprite(cr, ARROW, x + 2, ay + 3, px, None, None, shadow=True)
        _sprite(cr, ARROW, x, ay, px, theme.hand_fill, theme.hand_outline)
    elif style == "ring":
        rr, rg, rb = theme.accent
        r_ring = (60 - 38 * (1 - (1 - intro) ** 3)) * size
        cr.set_source_rgba(rr, rg, rb, 0.18 + 0.12 * pulse)
        cr.arc(x, y, r_ring + 6 * pulse * size, 0, 2 * math.pi)
        cr.fill()
        cr.set_line_width(3 * size)
        cr.set_source_rgba(rr, rg, rb, 0.95)
        cr.arc(x, y, r_ring, 0, 2 * math.pi)
        cr.stroke()
        cr.arc(x, y, 6 * size, 0, 2 * math.pi)
        cr.fill()
    else:  # dot
        rr, rg, rb = theme.accent
        for k, a in ((3.2, 0.10 + 0.08 * pulse), (2.0, 0.22), (1.0, 1.0)):
            cr.set_source_rgba(rr, rg, rb, a)
            cr.arc(x, y + drop, 7 * size * k * (1 + 0.15 * pulse * (k > 1)), 0, 2 * math.pi)
            cr.fill()


# ---------------------------------------------------------------- themes

class Theme:
    key = "base"
    name = "Base"
    pointer = "hand"
    hand_fill = (1, 1, 1)
    hand_outline = (0, 0, 0)
    accent = (0.25, 0.6, 1.0)
    tap = (1.0, 0.25, 0.2)
    pen = (1.0, 0.25, 0.2)
    placeholder = "ask about your screen…"
    backdrop = None   # {"radius": r}: wants a real blurred-glass backdrop behind the card where the platform has one

    def box_css(self):
        return ""

    def hit_regions(self, card, x, y, w, h, opts=None):
        """{control name: (x, y, w, h)} clickable on this card. Empty = fully click-through."""
        return {}

    def size(self, card, opts):
        raise NotImplementedError

    def draw(self, cr, x, y, w, h, card, t, opts):
        raise NotImplementedError


class Midnight(Theme):
    key, name = "midnight", "Midnight"
    bg = (0.094, 0.094, 0.11)
    border = (0.35, 0.63, 1.0)
    fg = (0.95, 0.95, 0.95)
    head = (0.54, 0.72, 1.0)
    err = (1.0, 0.54, 0.5)
    font = "Noto Sans"
    radius = 14

    def _width(self, card):
        return 360 if card.follow else 520

    def _layouts(self, cr, card, opts):
        w = self._width(card)
        body = layout(cr, card.text or " ", self.font, opts["text_size"], width=w)
        head = layout(cr, card.header, self.font, opts["text_size"] - 2, bold=True) if card.header else None
        return body, head

    CTRL_H = 34
    CTRL_MIN_W = 310

    def size(self, card, opts):
        body, head = self._layouts(_measure_ctx, card, opts)
        bw, bh = lsize(body)
        hh = lsize(head)[1] + 3 if head else 0
        w, h = max(bw, lsize(head)[0] if head else 0) + 32, bh + hh + 24
        if lean_controls(card, opts):
            w, h = max(w, self.CTRL_MIN_W), h + self.CTRL_H
        return w, h

    def draw(self, cr, x, y, w, h, card, t, opts):
        round_rect(cr, x + 0.5, y + 0.5, w - 1, h - 1, self.radius)
        cr.set_source_rgba(*self.bg, opts["card_opacity"])
        cr.fill_preserve()
        cr.set_source_rgba(*self.border, 0.55)
        cr.set_line_width(1)
        cr.stroke()
        body, head = self._layouts(cr, card, opts)
        ty = y + 12
        if head:
            show(cr, head, x + 16, ty, (*self.head, 1))
            ty += lsize(head)[1] + 3
        if thinking(card):  # the dots cycle, and a glow glides along the bottom edge
            show(cr, layout(cr, thinking_word(t), self.font, opts["text_size"]), x + 16, ty, (*self.fg, 1))
            cr.save()
            round_rect(cr, x + 1, y + 1, w - 2, h - 2, self.radius)
            cr.clip()
            sweep(cr, x, y + h - 3, w, 3, t, (*self.head, 0.9))
            cr.restore()
            return
        show(cr, body, x + 16, ty, (*(self.err if card.error else self.fg), 1))
        if lean_controls(card, opts):
            self._draw_controls(cr, x, y, w, h, card, opts)

    def _mgeom(self, x, y, w, h, card):
        cy = y + h - self.CTRL_H / 2 - 4
        px0 = x + w - 16 - 122
        return {"cy": cy, "n": len(card.steps), "dot0": x + 22,
                "prev": (px0 + 14, cy), "toggle": (px0 + 42, cy), "next": (px0 + 70, cy),
                "chip": (px0 + 86, cy - 10, 36, 20)}

    def _draw_controls(self, cr, x, y, w, h, card, opts):
        G = self._mgeom(x, y, w, h, card)
        cy, n = G["cy"], G["n"]
        pressed = opts.get("pressed")
        # step dots: past dimmed, current filled accent, future hollow
        if n > 1:
            for i in range(n):
                dx = G["dot0"] + i * 14
                cr.arc(dx, cy, 4.5 if i == card.step else 3.5, 0, 2 * math.pi)
                if i == card.step:
                    cr.set_source_rgba(*self.head, 1)
                    cr.fill()
                elif i < card.step:
                    cr.set_source_rgba(*self.fg, 0.45)
                    cr.fill()
                else:
                    cr.set_source_rgba(*self.fg, 0.45)
                    cr.set_line_width(1)
                    cr.stroke()
        # slim pill: prev / play-pause / next + speed chip
        px0 = G["prev"][0] - 14
        round_rect(cr, px0 - 4, cy - 13, 134, 26, 13)
        cr.set_source_rgba(*self.fg, 0.06)
        cr.fill_preserve()
        cr.set_source_rgba(*self.border, 0.35)
        cr.set_line_width(1)
        cr.stroke()
        for name in ("prev", "toggle", "next"):
            bx, by = G[name]
            if pressed == name:
                cr.arc(bx, by, 11, 0, 2 * math.pi)
                cr.set_source_rgba(*self.head, 0.3)
                cr.fill()
            g = name if name != "toggle" else ("pause" if playing(card) else "play")
            glyph(cr, g, bx, by, 5.5, (*self.fg, 0.95))
        cx_, cy_, cw_, ch_ = G["chip"]
        round_rect(cr, cx_, cy_, cw_, ch_, 10)
        cr.set_source_rgba(*self.head, 0.35 if pressed == "speed_cycle" else 0.18)
        cr.fill()
        sl = layout(cr, f"{card.speed:g}×", self.font, 11, bold=True)
        sw, sh = lsize(sl)
        show(cr, sl, cx_ + (cw_ - sw) / 2, cy_ + (ch_ - sh) / 2, (*self.head, 1))
        # thin progress line hugging the bottom edge
        cr.save()
        round_rect(cr, x + 1, y + 1, w - 2, h - 2, self.radius)
        cr.clip()
        cr.rectangle(x, y + h - 3, w * max(0.0, min(card.progress, 1.0)), 3)
        cr.set_source_rgba(*self.head, 0.9)
        cr.fill()
        cr.restore()

    def hit_regions(self, card, x, y, w, h, opts=None):
        if not lean_controls(card, opts or {}):
            return {}
        G = self._mgeom(x, y, w, h, card)
        cy = G["cy"]
        hits = {name: (G[name][0] - 12, cy - 12, 24, 24) for name in ("prev", "toggle", "next")}
        hits["speed_cycle"] = G["chip"]
        if G["n"] > 1:
            hits["seek"] = (G["dot0"] - 7, cy - 9, G["n"] * 14, 18)
        return hits

    def box_css(self):
        return """
window.flippy-box .flippy-card { background: rgba(24,24,28,0.94); border: 1px solid rgba(90,160,255,0.55);
  border-radius: 14px; padding: 12px 16px; color: #f2f2f2; }
window.flippy-box entry { font-size: 16px; }
window.flippy-box .flippy-hint { color: rgba(255,255,255,0.5); font-size: 12px; }"""


class Cosmic(Midnight):
    """Follows the COSMIC desktop's accent color, light/dark mode and corner radius (on macOS: the system's)."""
    key, name = "cosmic", "Follow macOS" if sys.platform == "darwin" else "Follow COSMIC"

    def __init__(self):
        # Importing controller/tests must not initialize NSApplication. The
        # controller refreshes this theme after the native UI has started.
        self.dark = True
        self.accent = self.border = self.head = (0.39, 0.82, 0.87)
        self.bg, self.fg, self.radius = (0.11,) * 3, (0.95,) * 3, 16

    def refresh(self):
        if sys.platform == "darwin":  # same idea on macOS: the system accent color and light/dark mode
            from .mac.system import appearance
            self.dark, self.accent, self.bg = appearance()
            self.border = self.head = self.accent
            self.fg = (0.95, 0.95, 0.95) if self.dark else (0.1, 0.1, 0.12)
            self.radius = 14
            return
        base = os.path.expanduser("~/.config/cosmic")
        try:
            dark = open(f"{base}/com.system76.CosmicTheme.Mode/v1/is_dark").read().strip() == "true"
        except OSError:
            dark = True
        d = f"{base}/com.system76.CosmicTheme.{'Dark' if dark else 'Light'}/v1"

        def color(fname, path=("base",)):
            try:
                s = open(f"{d}/{fname}").read()
                for p in path:
                    s = s[s.index(p + ":"):]
                vals = [float(re.search(rf"{c}:\s*([0-9.]+)", s).group(1)) for c in ("red", "green", "blue")]
                return tuple(vals)
            except (OSError, ValueError, AttributeError):
                return None
        self.accent = color("accent") or (0.39, 0.82, 0.87)
        self.border = self.head = self.accent
        self.bg = color("background") or ((0.11,) * 3 if dark else (0.95,) * 3)
        self.fg = (0.95, 0.95, 0.95) if dark else (0.1, 0.1, 0.12)
        self.dark = dark
        try:
            m = re.search(r"radius_m:\s*\(([0-9.]+)", open(f"{d}/corner_radii").read())
            self.radius = float(m.group(1))
        except (OSError, AttributeError):
            self.radius = 16

    CTRL_H = 72

    def _cgeom(self, x, y, w, h):
        row = y + h - self.CTRL_H - 2
        ox, cy = x + w / 2, row + 46
        return {"row": row, "cy": cy, "ox": ox, "x0": x + 18, "x1": x + w - 18,
                "prev": (ox - 48, cy), "toggle": (ox, cy), "next": (ox + 48, cy),
                "chip": (x + 16, cy - 12, 42, 24), "close": (x + w - 16 - 24, cy - 12, 24, 24)}

    def _draw_controls(self, cr, x, y, w, h, card, opts):
        G = self._cgeom(x, y, w, h)
        pressed = opts.get("pressed")
        comp = tuple(min(c + (0.08 if self.dark else -0.08), 1) for c in self.bg)
        # slider: rounded track, accent fill, round knob (COSMIC style)
        ty = G["row"] + 14
        x0, x1 = G["x0"], G["x1"]
        k = max(0.0, min(card.progress, 1.0))
        round_rect(cr, x0, ty - 2, x1 - x0, 4, 2)
        cr.set_source_rgba(*self.fg, 0.15)
        cr.fill()
        round_rect(cr, x0, ty - 2, max((x1 - x0) * k, 4), 4, 2)
        cr.set_source_rgba(*self.accent, 1)
        cr.fill()
        kx = x0 + (x1 - x0) * k
        cr.arc(kx, ty, 8 if pressed == "seek" else 7, 0, 2 * math.pi)
        cr.set_source_rgb(*self.accent)
        cr.fill()
        cr.arc(kx, ty, 3, 0, 2 * math.pi)
        cr.set_source_rgb(*self.bg)
        cr.fill()
        # icon buttons; the middle one is a filled accent circle
        for name in ("prev", "next"):
            bx, by = G[name]
            if pressed == name:
                cr.arc(bx, by, 17, 0, 2 * math.pi)
                cr.set_source_rgba(*self.fg, 0.12)
                cr.fill()
            glyph(cr, name, bx, by, 6.5, (*self.fg, 0.9))
        bx, by = G["toggle"]
        cr.arc(bx, by, 20, 0, 2 * math.pi)
        cr.set_source_rgba(*self.accent, 0.8 if pressed == "toggle" else 1)
        cr.fill()
        glyph(cr, "pause" if playing(card) else "play", bx + (0 if playing(card) else 1.5), by, 7, (*self.bg, 1))
        cx_, cy_, cw_, ch_ = G["chip"]
        round_rect(cr, cx_, cy_, cw_, ch_, min(self.radius, 12))
        cr.set_source_rgba(*comp, 1)
        cr.fill()
        sl = layout(cr, f"{card.speed:g}×", self.font, 12, bold=True)
        sw, sh = lsize(sl)
        show(cr, sl, cx_ + (cw_ - sw) / 2, cy_ + (ch_ - sh) / 2, (*(self.accent if pressed == "speed_cycle" else self.fg), 1))
        cx_, cy_, cw_, ch_ = G["close"]
        cr.arc(cx_ + cw_ / 2, cy_ + ch_ / 2, 12, 0, 2 * math.pi)
        cr.set_source_rgba(*comp, 1)
        cr.fill()
        glyph(cr, "close", cx_ + cw_ / 2, cy_ + ch_ / 2, 4.5, (*self.fg, 0.85))

    def hit_regions(self, card, x, y, w, h, opts=None):
        if not lean_controls(card, opts or {}):
            return {}
        G = self._cgeom(x, y, w, h)
        hits = {name: (G[name][0] - 18, G[name][1] - 18, 36, 36) for name in ("prev", "toggle", "next")}
        hits["seek"] = (G["x0"] - 8, G["row"] + 4, G["x1"] - G["x0"] + 16, 20)
        hits["speed_cycle"] = G["chip"]
        hits["close"] = G["close"]
        return hits

    def box_css(self):
        def css(c, a=1):
            return f"rgba({int(c[0] * 255)},{int(c[1] * 255)},{int(c[2] * 255)},{a})"
        return f"""
window.flippy-box .flippy-card {{ background: {css(self.bg, 0.96)}; border: 1px solid {css(self.accent, 0.7)};
  border-radius: {self.radius:.0f}px; padding: 12px 16px; color: {css(self.fg)}; }}
window.flippy-box entry {{ font-size: 16px; }}
window.flippy-box .flippy-hint {{ color: {css(self.fg, 0.5)}; font-size: 12px; }}"""


class Terminal(Theme):
    key, name = "terminal", "Terminal"
    pointer = "arrow"
    hand_fill = (0.0, 0.0, 0.0)
    hand_outline = (0.2, 1.0, 0.4)
    accent = (0.2, 1.0, 0.4)
    tap = (0.2, 1.0, 0.4)
    pen = (0.2, 1.0, 0.4)
    green = (0.25, 1.0, 0.45)
    dim = (0.15, 0.6, 0.3)
    font = "Fira Mono"
    placeholder = "$ ask "

    def _layouts(self, cr, card, opts):
        px = opts["text_size"] - 1
        w = 380 if card.follow else 540
        cursor = "█" if (card.typing or card.phase == "thinking") else ""
        body = layout(cr, (card.text or "") + cursor, self.font, px, width=w)
        head = layout(cr, f"> {card.header}", self.font, px, bold=True) if card.header else None
        bar = layout(cr, PROMPT, self.font, px - 3)
        return body, head, bar

    BAR_CHARS = 12

    def _status(self, cr, card, opts):
        """[(name or None, text, inverse)] tokens of the status line."""
        n = max(len(card.steps), 1)
        fill = round(max(0.0, min(card.progress, 1.0)) * self.BAR_CHARS)
        return [("prev", "[<<]"), (None, " "), ("toggle", "[||]" if playing(card) else "[|>]"), (None, " "),
                ("next", "[>>]"), (None, " "), ("close", "[x]"), (None, "  "),
                ("speed_cycle", f"speed {card.speed:g}x"), (None, "  "),
                ("seek", "█" * fill + "░" * (self.BAR_CHARS - fill)), (None, f" {card.step + 1}/{n}")]

    def _status_layout(self, cr, card, opts, x, y):
        """Token layouts with their positions: [(name, layout, x, w, h)]."""
        px = opts["text_size"] - 2
        out, cx = [], x
        for name, text in self._status(cr, card, opts):
            lay = layout(cr, text, self.font, px)
            tw, th = lsize(lay)
            out.append((name, lay, cx, tw, th))
            cx += tw
        return out

    def size(self, card, opts):
        body, head, bar = self._layouts(_measure_ctx, card, opts)
        bw, bh = lsize(body)
        hh = lsize(head)[1] + 4 if head else 0
        w, h = max(bw, 200) + 28, bh + hh + lsize(bar)[1] + 30
        if lean_controls(card, opts):
            toks = self._status_layout(_measure_ctx, card, opts, 0, 0)
            w = max(w, toks[-1][2] + toks[-1][3] + 28)
            h += toks[0][4] + 12
        return w, h

    def _status_y(self, card, opts, y, h):
        toks = self._status_layout(_measure_ctx, card, opts, 0, 0)
        return y + h - toks[0][4] - 7

    def hit_regions(self, card, x, y, w, h, opts=None):
        opts = opts or {}
        if not lean_controls(card, opts):
            return {}
        sy = self._status_y(card, opts, y, h)
        return {name: (tx, sy - 2, tw, th + 4) for name, _, tx, tw, th in
                self._status_layout(_measure_ctx, card, opts, x + 14, sy) if name}

    def draw(self, cr, x, y, w, h, card, t, opts):
        body, head, bar = self._layouts(cr, card, opts)
        cr.rectangle(x, y, w, h)
        cr.set_source_rgba(0.01, 0.03, 0.02, opts["card_opacity"])
        cr.fill()
        bh = lsize(bar)[1] + 6
        cr.rectangle(x, y, w, bh)
        cr.set_source_rgba(0.05, 0.15, 0.08, 1)
        cr.fill()
        show(cr, bar, x + 10, y + 3, (*self.dim, 1))
        cr.rectangle(x + 0.5, y + 0.5, w - 1, h - 1)
        cr.set_source_rgba(*self.green, 0.6)
        cr.set_line_width(1)
        cr.stroke()
        ty = y + bh + 8
        if head:
            show(cr, head, x + 14, ty, (*self.green, 1))
            ty += lsize(head)[1] + 4
        blink = (card.typing or card.phase == "thinking") and int(t * 2.5) % 2
        if thinking(card):  # a spinning bar, like a CLI waiting on a request
            spin = "|/-\\"[int(t * 8) % 4]
            body = layout(cr, f"{spin} {thinking_word(t)}" + ("" if blink else " █"), self.font,
                          opts["text_size"] - 1, width=380 if card.follow else 540)
        elif blink:  # hide the block cursor every other beat
            body = layout(cr, card.text or " ", self.font, opts["text_size"] - 1, width=380 if card.follow else 540)
        show(cr, body, x + 14, ty, (1.0, 0.45, 0.4, 1) if card.error else (*self.green, 1))
        if lean_controls(card, opts):
            sy = self._status_y(card, opts, y, h)
            cr.rectangle(x + 1, sy - 6, w - 2, 1)
            cr.set_source_rgba(*self.dim, 0.7)
            cr.fill()
            pressed = opts.get("pressed")
            for name, lay, tx, tw, th in self._status_layout(cr, card, opts, x + 14, sy):
                inverse = name and (name == pressed or (name == "toggle" and card.paused))
                if inverse:
                    cr.rectangle(tx, sy - 1, tw, th + 2)
                    cr.set_source_rgba(*self.green, 1)
                    cr.fill()
                color = (0.0, 0.05, 0.02, 1) if inverse else ((*self.green, 1) if name else (*self.dim, 1))
                show(cr, lay, tx, sy, color)

    def box_css(self):
        return """
window.flippy-box .flippy-card { background: #030805; border: 1px solid rgba(64,255,115,0.6); border-radius: 0;
  padding: 10px 14px; color: #40ff73; font-family: "Fira Mono", monospace; }
window.flippy-box entry { background: #000; color: #40ff73; caret-color: #40ff73; border-radius: 0;
  font-family: "Fira Mono", monospace; font-size: 16px; border: 1px solid rgba(64,255,115,0.35); }
window.flippy-box .flippy-hint { color: rgba(64,255,115,0.55); font-size: 12px; font-family: "Fira Mono", monospace; }"""


# ---- Y2K media player ------------------------------------------------------

_SEG = {  # 7-segment: a b c d e f g
    "0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc", "5": "afgcd",
    "6": "afgedc", "7": "abc", "8": "abcdefg", "9": "abcfgd", "-": "g", " ": "",
}


def seven_seg(cr, text, x, y, dw, dh, rgb, glow=True):
    """Draw text as LCD 7-segment digits; returns width used."""
    t = max(2.0, dw * 0.18)
    segs = {
        "a": (x + t, y, dw - 2 * t, t), "d": (x + t, y + dh - t, dw - 2 * t, t),
        "g": (x + t, y + dh / 2 - t / 2, dw - 2 * t, t),
        "f": (x, y + t, t, dh / 2 - t * 1.5), "b": (x + dw - t, y + t, t, dh / 2 - t * 1.5),
        "e": (x, y + dh / 2 + t / 2, t, dh / 2 - t * 1.5), "c": (x + dw - t, y + dh / 2 + t / 2, t, dh / 2 - t * 1.5),
    }
    cx = 0
    for ch in text:
        if ch in ":/":
            cr.set_source_rgba(*rgb, 1)
            if ch == ":":
                for yy in (y + dh * 0.3, y + dh * 0.65):
                    cr.rectangle(x + cx + 1, yy, t, t)
            else:
                cr.move_to(x + cx + dw * 0.55, y)
                cr.line_to(x + cx + t, y + dh)
                cr.set_line_width(t * 0.8)
                cr.stroke()
            cr.fill()
            cx += dw * 0.6
            continue
        for s in "abcdefg":  # unlit segments, faintly
            sx, sy, sw, sh = segs[s]
            cr.rectangle(sx + cx, sy, sw, sh)
        cr.set_source_rgba(*rgb, 0.08)
        cr.fill()
        on = _SEG.get(ch, "")
        if glow:
            for s in on:
                sx, sy, sw, sh = segs[s]
                cr.rectangle(sx + cx - 1.5, sy - 1.5, sw + 3, sh + 3)
            cr.set_source_rgba(*rgb, 0.25)
            cr.fill()
        for s in on:
            sx, sy, sw, sh = segs[s]
            cr.rectangle(sx + cx, sy, sw, sh)
        cr.set_source_rgba(*rgb, 1)
        cr.fill()
        cx += dw + 3
    return cx


def bevel(cr, x, y, w, h, light, dark, width=1):
    cr.set_line_width(width)
    cr.set_source_rgb(*light)
    cr.move_to(x + 0.5, y + h - 0.5)
    cr.line_to(x + 0.5, y + 0.5)
    cr.line_to(x + w - 0.5, y + 0.5)
    cr.stroke()
    cr.set_source_rgb(*dark)
    cr.move_to(x + w - 0.5, y + 0.5)
    cr.line_to(x + w - 0.5, y + h - 0.5)
    cr.line_to(x + 0.5, y + h - 0.5)
    cr.stroke()


class Y2K(Theme):
    key, name = "y2k", "Y2K Player"
    pointer = "hand"
    hand_fill = (0.72, 1.0, 0.69)
    hand_outline = (0.02, 0.08, 0.02)
    accent = (0.0, 1.0, 0.42)
    tap = (0.0, 1.0, 0.42)
    pen = (0.22, 1.0, 0.08)
    placeholder = "ENTER QUERY..."
    LCD = (0.0, 1.0, 0.42)
    LIST = (0.0, 0.88, 0.44)
    BODY = (0.66, 1.0, 0.75)
    HL = (0.04, 0.23, 0.61)
    METAL_HI = (0.61, 0.63, 0.70)
    METAL_LO = (0.05, 0.05, 0.07)
    TITLE_H, LCD_H, BTN_H, PAD = 20, 54, 30, 6

    def _w(self, card):
        return 400 if card.follow else 460

    THINK_H = 34      # the LCD while thinking: a line of text and the visualizer

    def hit_regions(self, card, x, y, w, h, opts=None):
        P = self.PAD
        if card.phase == "thinking":  # only the window buttons: there's nothing to play yet
            return {"close": (x + w - 17, y + 8, 10, 10), "min": (x + w - 29, y + 8, 10, 10)}
        by = y + h - self.BTN_H - P + 4
        hits = {name: (x + P + i * 26, by, 23, 18) for i, name in enumerate(("prev", "play", "pause", "stop", "next"))}
        sx = x + P + 5 * 26 + 8
        hits["seek"] = (sx, by, x + w - P - sx, 18)
        hits["close"] = (x + w - 17, y + 8, 10, 10)
        hits["min"] = (x + w - 29, y + 8, 10, 10)
        ly = y + 3 + self.TITLE_H + P
        hits["speed_cycle"] = (x + w - P - 60, ly + 14, 56, 16)
        return hits

    def _playlist(self, cr, card, opts):
        """Rows: (text, kind) where kind is past|current|body|future."""
        px = opts["text_size"] + 4
        w = self._w(card) - 2 * self.PAD - 16
        rows = []
        labels = card.steps or [card.header or ("ERROR" if card.error else "ANSWER")]
        cur = min(card.step, len(labels) - 1)
        first = max(0, cur - 3)
        if first > 0:
            rows.append((layout(cr, "  ...", PIXEL_FONT, px), "past"))
        for i in range(first, cur + 1):
            rows.append((layout(cr, f"{i + 1}. {labels[i].upper()}", PIXEL_FONT, px, width=w), "past" if i < cur else "current"))
        rows.append((layout(cr, card.text or " ", PIXEL_FONT, px - 1, width=w - 10), "body"))
        return rows

    def size(self, card, opts):
        if card.phase == "thinking":
            return self._w(card), 3 + self.TITLE_H + self.PAD + self.THINK_H + self.PAD + 3
        rows = self._playlist(_measure_ctx, card, opts)
        list_h = sum(lsize(r)[1] + (6 if k == "body" else 1) for r, k in rows) + 12
        return self._w(card), self.TITLE_H + self.LCD_H + list_h + self.BTN_H + self.PAD * 4

    def draw(self, cr, x, y, w, h, card, t, opts):
        P = self.PAD
        a = opts["card_opacity"]
        # body: brushed dark metal
        g = cairo.LinearGradient(0, y, 0, y + h)
        g.add_color_stop_rgba(0, 0.20, 0.21, 0.27, a)
        g.add_color_stop_rgba(1, 0.11, 0.12, 0.16, a)
        cr.rectangle(x, y, w, h)
        cr.set_source(g)
        cr.fill()
        bevel(cr, x, y, w, h, self.METAL_HI, self.METAL_LO, 2)
        # title bar with grip lines
        tx, ty, tw = x + 3, y + 3, w - 6
        g = cairo.LinearGradient(0, ty, 0, ty + self.TITLE_H)
        g.add_color_stop_rgb(0, 0.27, 0.29, 0.36)
        g.add_color_stop_rgb(1, 0.13, 0.14, 0.19)
        cr.rectangle(tx, ty, tw, self.TITLE_H)
        cr.set_source(g)
        cr.fill()
        title = layout(cr, "FLIPPY", PIXEL_FONT, 17)
        tl_w, tl_h = lsize(title)
        mid = tx + tw / 2
        for gx0, gx1 in ((tx + 22, mid - tl_w / 2 - 8), (mid + tl_w / 2 + 8, tx + tw - 34)):
            for k in range(4):
                yy = ty + 5 + k * 3
                cr.set_source_rgb(0.42, 0.44, 0.52)
                cr.rectangle(gx0, yy, max(gx1 - gx0, 0), 1)
                cr.fill()
        show(cr, title, mid - tl_w / 2, ty + (self.TITLE_H - tl_h) / 2, (0.85, 0.87, 0.93, 1))
        menu = layout(cr, "≡", PIXEL_FONT, 16)
        show(cr, menu, tx + 6, ty + 1, (0.75, 0.78, 0.86, 1))
        for i, glyph in enumerate(("_", "x")):  # fake window buttons
            bx = tx + tw - 26 + i * 12
            cr.rectangle(bx, ty + 5, 10, 10)
            cr.set_source_rgb(0.55, 0.57, 0.64)
            cr.fill()
            bevel(cr, bx, ty + 5, 10, 10, (0.85, 0.86, 0.9), (0.15, 0.15, 0.2))
            gl = layout(cr, glyph, PIXEL_FONT, 12)
            show(cr, gl, bx + 2, ty + 3, (0.05, 0.05, 0.08, 1))
        # LCD
        lx, ly, lw = x + P, ty + self.TITLE_H + P, w - 2 * P
        if card.phase == "thinking":
            self._thinking_lcd(cr, lx, ly, lw, card, t)
            return
        cr.rectangle(lx, ly, lw, self.LCD_H)
        cr.set_source_rgb(0, 0, 0)
        cr.fill()
        bevel(cr, lx, ly, lw, self.LCD_H, self.METAL_LO, self.METAL_HI)
        n = max(len(card.steps), 1)
        digits = f"{min(card.step + 1, 99):02d}/{min(n, 99):02d}"
        play = self.LCD if not card.error else (1, 0.3, 0.25)
        cr.move_to(lx + 8, ly + 9)
        cr.line_to(lx + 8, ly + 21)
        cr.line_to(lx + 16, ly + 15)
        cr.close_path()
        cr.set_source_rgb(*play)
        cr.fill()
        dw = seven_seg(cr, digits, lx + 22, ly + 6, 10, 20, play)
        self._visualizer(cr, lx + 30 + dw, ly + 6, 22, card.typing, t)
        meta = layout(cr, card.meta or "CLAUDE", PIXEL_FONT, 15)
        mw = lsize(meta)[0]
        show(cr, meta, lx + lw - mw - 8, ly + 4, (*self.LCD, 0.9))
        stereo = layout(cr, f"SPEED {card.speed:g}X", PIXEL_FONT, 13)
        show(cr, stereo, lx + lw - lsize(stereo)[0] - 8, ly + 18, (*self.LCD, 0.45))
        # marquee
        label = card.header or "FLIPPY - ANSWER"
        mq = layout(cr, f"♪ {card.step + 1}. {label.upper()}   ***   ", PIXEL_FONT, 17)
        mqw, mqh = lsize(mq)
        cr.save()
        cr.rectangle(lx + 6, ly + 31, lw - 12, mqh)
        cr.clip()
        off = (t * 40) % mqw if mqw > lw - 12 else 0
        for k in (0, 1) if mqw > lw - 12 else (0,):
            show(cr, mq, lx + 8 - off + k * mqw, ly + 31, (*self.LCD, 1))
        cr.restore()
        # playlist
        rows = self._playlist(cr, card, opts)
        px_, py_ = x + P, ly + self.LCD_H + P
        list_h = h - (py_ - y) - self.BTN_H - P * 2
        cr.rectangle(px_, py_, lw, list_h)
        cr.set_source_rgb(0, 0, 0)
        cr.fill()
        bevel(cr, px_, py_, lw, list_h, self.METAL_LO, self.METAL_HI)
        ry = py_ + 6
        for lay, kind in rows:
            rw, rh = lsize(lay)
            if kind == "current":
                cr.rectangle(px_ + 3, ry - 1, lw - 6, rh + 2)
                cr.set_source_rgb(*((0.45, 0.06, 0.05) if card.error else self.HL))
                cr.fill()
                show(cr, lay, px_ + 8, ry, (1, 1, 1, 1))
            elif kind == "past":
                show(cr, lay, px_ + 8, ry, (*self.LIST, 0.55))
            else:
                show(cr, lay, px_ + 18, ry + 2, (1.0, 0.55, 0.5, 1) if card.error else (*self.BODY, 1))
                ry += 5
            ry += rh + 1
        # transport buttons + seek bar
        by = y + h - self.BTN_H - P + 4
        glyphs = ["prev", "play", "pause", "stop", "next"]
        pressed = opts.get("pressed")
        for i, gname in enumerate(glyphs):
            bx = x + P + i * 26
            g = cairo.LinearGradient(0, by, 0, by + 18)
            down = pressed == gname or (gname == "pause" and card.paused) or (gname == "play" and playing(card))
            g.add_color_stop_rgb(0, *((0.52, 0.54, 0.62) if down else (0.80, 0.82, 0.88)))
            g.add_color_stop_rgb(1, *((0.74, 0.76, 0.82) if down else (0.52, 0.54, 0.62)))
            cr.rectangle(bx, by, 23, 18)
            cr.set_source(g)
            cr.fill()
            bevel(cr, bx, by, 23, 18, (0.95, 0.96, 1.0), (0.12, 0.12, 0.16))
            cr.set_source_rgb(0.06, 0.06, 0.09)
            cx_, cy_ = bx + 11.5, by + 9
            if gname == "play":
                cr.move_to(cx_ - 3, cy_ - 4)
                cr.line_to(cx_ - 3, cy_ + 4)
                cr.line_to(cx_ + 4, cy_)
                cr.close_path()
            elif gname == "pause":
                cr.rectangle(cx_ - 4, cy_ - 4, 3, 8)
                cr.rectangle(cx_ + 1, cy_ - 4, 3, 8)
            elif gname == "stop":
                cr.rectangle(cx_ - 4, cy_ - 4, 8, 8)
            else:
                d = -1 if gname == "prev" else 1
                cr.rectangle(cx_ + d * 4 - (2 if d > 0 else 0), cy_ - 4, 2, 8)
                cr.move_to(cx_ - d * 3, cy_ - 4)
                cr.line_to(cx_ - d * 3, cy_ + 4)
                cr.line_to(cx_ + d * 2, cy_)
                cr.close_path()
            cr.fill()
        sx = x + P + len(glyphs) * 26 + 8
        sw = x + w - P - sx
        cr.rectangle(sx, by + 6, sw, 6)
        cr.set_source_rgb(0.03, 0.03, 0.05)
        cr.fill()
        bevel(cr, sx, by + 6, sw, 6, self.METAL_LO, self.METAL_HI)
        prog = max(0.0, min(card.progress, 1.0))
        cr.rectangle(sx + 1, by + 7, (sw - 2) * prog, 4)
        cr.set_source_rgb(*self.LCD)
        cr.fill()
        thx = sx + (sw - 14) * prog
        g = cairo.LinearGradient(0, by + 2, 0, by + 16)
        g.add_color_stop_rgb(0, 0.85, 0.87, 0.92)
        g.add_color_stop_rgb(1, 0.5, 0.52, 0.6)
        cr.rectangle(thx, by + 2, 14, 14)
        cr.set_source(g)
        cr.fill()
        bevel(cr, thx, by + 2, 14, 14, (0.97, 0.97, 1.0), (0.1, 0.1, 0.14))

    @staticmethod
    def _visualizer(cr, vx, vy, vh, active, t):
        """The spectrum bars: busy while text comes in (or Flippy thinks), idling otherwise."""
        for i in range(14):
            amp = 0.85 if active else 0.25
            v = 0.5 + 0.5 * math.sin(t * (5.0 + i * 0.7) + i * 1.3) * math.sin(t * 2.3 + i)
            hgt = max(1, int(amp * v * (vh / 2)))
            for b in range(hgt):
                k = b / (vh / 2)
                col = (0.0, 1.0, 0.3) if k < 0.5 else ((0.9, 1.0, 0.0) if k < 0.8 else (1.0, 0.25, 0.1))
                cr.set_source_rgb(*col)
                cr.rectangle(vx + i * 6, vy + vh - b * 2 - 2, 4, 1.5)
                cr.fill()

    def _thinking_lcd(self, cr, lx, ly, lw, card, t):
        """While Flippy thinks: a short LCD with the card's text (thinking…) and the visualizer going."""
        h = self.THINK_H
        cr.rectangle(lx, ly, lw, h)
        cr.set_source_rgb(0, 0, 0)
        cr.fill()
        bevel(cr, lx, ly, lw, h, self.METAL_LO, self.METAL_HI)
        text = layout(cr, thinking_word(t).upper(), PIXEL_FONT, 20)
        tw_, th_ = lsize(text)
        show(cr, text, lx + 10, ly + (h - th_) / 2, (*self.LCD, 1))
        if int(t * 2.5) % 2:  # a blinking block cursor after it
            cr.rectangle(lx + 12 + tw_, ly + h / 2 - 7, 8, 14)
            cr.set_source_rgb(*self.LCD)
            cr.fill()
        self._visualizer(cr, lx + lw - 14 * 6 - 8, ly + 6, h - 12, True, t)

    def box_css(self):
        return f"""
window.flippy-box .flippy-card {{ background: linear-gradient(#454959, #22252f); border-radius: 2px;
  border: 2px solid; border-color: #9ca1b3 #0c0d12 #0c0d12 #9ca1b3; padding: 10px 12px; color: #c8ccd8; }}
window.flippy-box entry {{ background: #000; color: #00ff6a; caret-color: #00ff6a; border-radius: 0;
  font-family: "{PIXEL_FONT}"; font-size: 24px; border: 2px solid; border-color: #0c0d12 #9ca1b3 #9ca1b3 #0c0d12;
  box-shadow: none; text-shadow: 0 0 6px rgba(0,255,106,0.5); }}
window.flippy-box .flippy-hint {{ color: #7dffa9; font-family: "{PIXEL_FONT}"; font-size: 16px; }}"""



# ---- Media Player (2000s media player mini mode: dark teal glass, glossy orb) -----

class MediaPlayer(Theme):
    key, name = "mediaplayer", "Media Player"
    pointer = "arrow"
    hand_fill = (1, 1, 1)
    hand_outline = (0, 0, 0)
    accent = (0.35, 0.72, 1.0)
    tap = (0.35, 0.72, 1.0)
    pen = (0.3, 0.7, 1.0)
    placeholder = "Search or ask…"
    font = "Noto Sans"
    TITLE_H, CTRL_H, R = 26, 46, 9
    LIQUID_R = 20
    backdrop = {"radius": LIQUID_R}  # macOS: Liquid Glass (NSGlassEffectView) behind the card

    def _w(self, card):
        return 380 if card.follow else 470

    def _body(self, cr, card, opts):
        return layout(cr, card.text or " ", self.font, opts["text_size"], width=self._w(card) - 44)

    def _geom(self, x, y, w, h):
        cy = y + h - self.CTRL_H / 2 - 2
        ox = x + w / 2
        bx = x + w - 9 - 3 * 22
        sy = y + h - self.CTRL_H - 4
        vx = ox + 76
        vs0, vs1 = vx + 22, min(vx + 66, x + w - 16)
        return {"cy": cy, "ox": ox, "bx": bx, "sy": sy, "vx": vx, "vs0": vs0, "vs1": vs1,
                "sx0": x + 14, "sx1": x + w - 14}

    def hit_regions(self, card, x, y, w, h, opts=None):
        if opts and opts.get("backdrop"):  # same inset as _chrome
            x, w = x + 4, w - 8
        G = self._geom(x, y, w, h)
        cy, ox, bx = G["cy"], G["ox"], G["bx"]
        window = {"min": (bx, y + 5, 21, 15), "max": (bx + 22, y + 5, 21, 15), "close": (bx + 44, y + 5, 24, 15)}
        if card.phase == "thinking":  # only the window buttons: there's nothing to play yet
            return window
        hits = {
            **window,
            "stop": (ox - 92, cy - 9, 22, 18),
            "prev": (ox - 62, cy - 11, 42, 22), "next": (ox + 20, cy - 11, 42, 22),
            "toggle": (ox - 18, cy - 18, 36, 36),
            "seek": (G["sx0"] - 4, G["sy"] - 6, G["sx1"] - G["sx0"] + 8, 14),
        }
        if G["vs1"] > G["vs0"] + 10:
            hits["speed"] = (G["vs0"] - 5, cy - 9, G["vs1"] - G["vs0"] + 10, 18)
        return hits

    def size(self, card, opts):
        _, bh = lsize(self._body(_measure_ctx, card, opts))
        if card.phase == "thinking":  # the title bar and the text: no controls yet
            return self._w(card), self.TITLE_H + bh + 30
        return self._w(card), self.TITLE_H + bh + 26 + self.CTRL_H + 8

    @staticmethod
    def _gloss_button(cr, x, y, w, h, r, base=(0.16, 0.22, 0.25), hi=0.35, pressed=False):
        if pressed:  # lit up while clicked
            base = (base[0] + 0.12, base[1] + 0.25, base[2] + 0.35)
        round_rect(cr, x, y, w, h, r)
        g = cairo.LinearGradient(0, y, 0, y + h)
        g.add_color_stop_rgba(0, base[0] + 0.25, base[1] + 0.25, base[2] + 0.25, 0.9)
        g.add_color_stop_rgba(0.5, base[0] + 0.05, base[1] + 0.05, base[2] + 0.05, 0.9)
        g.add_color_stop_rgba(0.51, *base, 0.95)
        g.add_color_stop_rgba(1, base[0] * 0.6, base[1] * 0.6, base[2] * 0.6, 0.95)
        cr.set_source(g)
        cr.fill_preserve()
        cr.set_source_rgba(0, 0, 0, 0.7)
        cr.set_line_width(1)
        cr.stroke()
        round_rect(cr, x + 1.5, y + 1.5, w - 3, h / 2 - 1, max(r - 1.5, 1))
        cr.set_source_rgba(1, 1, 1, hi * 0.35)
        cr.fill()

    def _liquid(self, cr, x, y, w, h, a, gloss=True, controls=True):
        """Over a real glass backdrop: just light, no body. WMP 11-style gloss on the title and controls bars
        (lighter top half, crisp edge, a bright line on top) and a rim around the glass."""
        R = self.LIQUID_R
        cr.save()
        round_rect(cr, x, y, w, h, R)
        cr.clip()
        # barely any tint: the glass underneath does the work (its frost is set in flippy/mac/ui.py)
        g = cairo.LinearGradient(0, y, 0, y + h)
        g.add_color_stop_rgba(0, 1, 1, 1, 0.02 * a)
        g.add_color_stop_rgba(1, 0, 0.05, 0.08, 0.07 * a)
        cr.set_source(g)
        cr.paint()
        bars = ((y, self.TITLE_H + 2),) + ((y + h - self.CTRL_H - 10, self.CTRL_H + 10),) * controls if gloss else ()
        for by, bh in bars:  # title bar, controls bar
            half = bh * 0.5
            g = cairo.LinearGradient(0, by, 0, by + half)
            g.add_color_stop_rgba(0, 1, 1, 1, 0.16)
            g.add_color_stop_rgba(1, 1, 1, 1, 0.05)
            cr.rectangle(x, by, w, half)
            cr.set_source(g)
            cr.fill()
            cr.rectangle(x, by + half, w, bh - half)  # below the crisp edge: a touch darker
            cr.set_source_rgba(0, 0, 0, 0.06)
            cr.fill()
            cr.rectangle(x, by + 0.5, w, 1)  # bright line along the top of the bar
            g = cairo.LinearGradient(x, 0, x + w, 0)
            g.add_color_stop_rgba(0, 1, 1, 1, 0.05)
            g.add_color_stop_rgba(0.5, 1, 1, 1, 0.45)
            g.add_color_stop_rgba(1, 1, 1, 1, 0.05)
            cr.set_source(g)
            cr.fill()
        cr.restore()
        # rim: bright along the top, fading down the sides, a little light again along the bottom
        round_rect(cr, x + 0.75, y + 0.75, w - 1.5, h - 1.5, R - 0.5)
        g = cairo.LinearGradient(0, y, 0, y + h)
        g.add_color_stop_rgba(0, 1, 1, 1, 0.6)
        g.add_color_stop_rgba(0.25, 1, 1, 1, 0.15)
        g.add_color_stop_rgba(0.75, 1, 1, 1, 0.08)
        g.add_color_stop_rgba(1, 1, 1, 1, 0.28)
        cr.set_source(g)
        cr.set_line_width(1.5)
        cr.stroke()

    def draw(self, cr, x, y, w, h, card, t, opts):
        a = opts["card_opacity"]
        liquid = opts.get("backdrop")
        if liquid:
            self._liquid(cr, x, y, w, h, a, gloss=opts.get("player_shine", "wmp") == "wmp",
                         controls=card.phase != "thinking")
        else:
            self._classic(cr, x, y, w, h, a)
        self._chrome(cr, x, y, w, h, card, t, opts, opts.get("pressed"), liquid)

    def _classic(self, cr, x, y, w, h, a):
        R = self.R
        # glass body: tinted, see-through, darker toward the bottom
        round_rect(cr, x, y, w, h, R)
        g = cairo.LinearGradient(0, y, 0, y + h)
        g.add_color_stop_rgba(0, 0.30, 0.50, 0.52, 0.34 * a)
        g.add_color_stop_rgba(0.45, 0.13, 0.31, 0.33, 0.42 * a)
        g.add_color_stop_rgba(1, 0.04, 0.14, 0.16, 0.60 * a)
        cr.set_source(g)
        cr.fill()
        # glossy shine on the top half, with a soft curved edge
        cr.save()
        round_rect(cr, x, y, w, h, R)
        cr.clip()
        cr.move_to(x, y)
        cr.line_to(x + w, y)
        cr.line_to(x + w, y + h * 0.30)
        cr.curve_to(x + w * 0.6, y + h * 0.40, x + w * 0.3, y + h * 0.36, x, y + h * 0.46)
        cr.close_path()
        g = cairo.LinearGradient(0, y, 0, y + h * 0.46)
        g.add_color_stop_rgba(0, 1, 1, 1, 0.22)
        g.add_color_stop_rgba(1, 1, 1, 1, 0.03)
        cr.set_source(g)
        cr.fill()
        cr.restore()
        # borders: dark outer, light inner
        round_rect(cr, x + 0.5, y + 0.5, w - 1, h - 1, R)
        cr.set_source_rgba(0, 0, 0, 0.75)
        cr.set_line_width(1)
        cr.stroke()
        round_rect(cr, x + 1.5, y + 1.5, w - 3, h - 3, R - 1)
        cr.set_source_rgba(1, 1, 1, 0.28)
        cr.stroke()

    @staticmethod
    def _label(cr, lay, x, y, rgba, liquid):
        """Small text on the controls row; on clear glass it gets a soft dark halo to read over light backgrounds."""
        if liquid:
            for dx, dy, al in ((0, 1, 0.6), (1, 0, 0.35), (-1, 0, 0.35), (0, -1, 0.25)):
                show(cr, lay, x + dx, y + dy, (0, 0, 0, al))
        show(cr, lay, x, y, rgba)

    def _chrome(self, cr, x, y, w, h, card, t, opts, pressed, liquid):
        a = opts["card_opacity"]
        if liquid:  # the rounder liquid corners: pull the edge controls in a little
            x, w = x + 4, w - 8
        # title bar: orange "now playing" icon, glowing title, window buttons
        ix, iy = x + 9, y + 6
        round_rect(cr, ix, iy, 14, 14, 3)
        g = cairo.LinearGradient(0, iy, 0, iy + 14)
        g.add_color_stop_rgb(0, 1.0, 0.78, 0.35)
        g.add_color_stop_rgb(1, 0.9, 0.42, 0.05)
        cr.set_source(g)
        cr.fill()
        cr.move_to(ix + 5, iy + 3.5)
        cr.line_to(ix + 5, iy + 10.5)
        cr.line_to(ix + 10.5, iy + 7)
        cr.close_path()
        cr.set_source_rgb(1, 1, 1)
        cr.fill()
        title = card.header or ("Error" if card.error else "Flippy")
        tl = layout(cr, title, self.font, 13, width=w - 120)
        ellipsize_end(tl)
        halo = ((0, 1, 0.7), (1, 0, 0.45), (-1, 0, 0.45), (0, -1, 0.3)) if liquid else ((0, 1, 0.55), (1, 0, 0.25), (-1, 0, 0.25))
        for dx, dy, al in halo:  # dark glow behind the text
            show(cr, tl, ix + 21 + dx, iy - 1 + dy, (0, 0, 0, al))
        show(cr, tl, ix + 21, iy - 1, (1, 1, 1, 1))
        bx = x + w - 9 - 3 * 22
        for i in range(3):  # min, max, close
            close = i == 2
            self._gloss_button(cr, bx + i * 22, y + 5, 21 if not close else 24, 15, 3,
                               base=(0.62, 0.12, 0.08) if close else (0.25, 0.32, 0.35), hi=0.5,
                               pressed=pressed == ("min", "max", "close")[i])
            cr.set_source_rgba(1, 1, 1, 0.9)
            cx_, cy_ = bx + i * 22 + (10.5 if not close else 12), y + 12.5
            cr.set_line_width(1.6)
            if i == 0:
                cr.move_to(cx_ - 3.5, cy_ + 3)
                cr.line_to(cx_ + 3.5, cy_ + 3)
            elif i == 1:
                cr.rectangle(cx_ - 3.5, cy_ - 3, 7, 6)
            else:
                cr.move_to(cx_ - 3.5, cy_ - 3)
                cr.line_to(cx_ + 3.5, cy_ + 3)
                cr.move_to(cx_ + 3.5, cy_ - 3)
                cr.line_to(cx_ - 3.5, cy_ + 3)
            cr.stroke()

        # body panel
        body = self._body(cr, card, opts)
        _, bh = lsize(body)
        px, py, pw, ph = x + 10, y + self.TITLE_H + 4, w - 20, bh + 16
        if liquid:  # the text sits on a darker, partly opaque pane so it reads over anything
            round_rect(cr, px, py, pw, ph, 12)
            g = cairo.LinearGradient(0, py, 0, py + ph)
            g.add_color_stop_rgba(0, 0.02, 0.05, 0.08, 0.50 * a)
            g.add_color_stop_rgba(1, 0.01, 0.03, 0.05, 0.62 * a)
            cr.set_source(g)
            cr.fill_preserve()
            cr.set_source_rgba(1, 1, 1, 0.16)
            cr.set_line_width(1)
            cr.stroke()
            round_rect(cr, px + 1, py + 1, pw - 2, 10, 11)  # inner top highlight
            cr.set_source_rgba(1, 1, 1, 0.05)
            cr.fill()
        else:
            round_rect(cr, px, py, pw, ph, 5)
            cr.set_source_rgba(0, 0.03, 0.05, 0.22)
            cr.fill_preserve()
            cr.set_source_rgba(1, 1, 1, 0.12)
            cr.set_line_width(1)
            cr.stroke()
        if thinking(card):
            body = layout(cr, thinking_word(t), self.font, opts["text_size"], width=self._w(card) - 44)
        for dx, dy in ((1, 1), (0, 1), (1, 0), (-1, 0), (0, -1)):
            show(cr, body, px + 12 + dx, py + 8 + dy, (0, 0, 0, 0.55))
        show(cr, body, px + 12, py + 8, (1.0, 0.62, 0.58, 1) if card.error else (0.97, 0.99, 1.0, 1))
        if card.phase == "thinking":  # no seek line or controls until there's an answer: a blue glow gliding instead
            sweep(cr, px + 6, py + ph - 3, pw - 12, 2, t, (0.55, 0.85, 1.0, 0.95), radius=1)
            return

        # seek line with a tick per walkthrough step and a glowing nub
        sy = y + h - self.CTRL_H - 4
        sx0, sx1 = x + 14, x + w - 14
        cr.rectangle(sx0, sy, sx1 - sx0, 2)
        cr.set_source_rgba(0, 0, 0, 0.55)
        cr.fill()
        prog = max(0.0, min(card.progress, 1.0))
        cr.rectangle(sx0, sy, (sx1 - sx0) * prog, 2)
        cr.set_source_rgba(0.45, 0.78, 1.0, 0.9)
        cr.fill()
        n = len(card.steps)
        for i in range(1, n):
            tx = sx0 + (sx1 - sx0) * i / n
            cr.rectangle(tx, sy - 2, 1, 6)
            cr.set_source_rgba(1, 1, 1, 0.35)
            cr.fill()
        nx = sx0 + (sx1 - sx0) * prog
        for rad, al in ((7, 0.18), (4.5, 0.45), (2.5, 1.0)):
            cr.arc(nx, sy + 1, rad, 0, 2 * math.pi)
            cr.set_source_rgba(0.55, 0.85, 1.0, al)
            cr.fill()

        # controls: counter, stop, prev|next pill, blue orb, volume
        cy = y + h - self.CTRL_H / 2 - 2
        counter = f"{card.step + 1} / {max(n, 1)}"
        cl = layout(cr, counter, self.font, 12)
        self._label(cr, cl, x + 14, cy - lsize(cl)[1] / 2, (0.9, 0.95, 1.0, 0.95), liquid)
        ox = x + w / 2
        # stop
        self._gloss_button(cr, ox - 92, cy - 9, 22, 18, 4, pressed=pressed == "stop")
        cr.rectangle(ox - 85, cy - 4, 8, 8)
        cr.set_source_rgba(0.55, 0.82, 1.0, 1)
        cr.fill()
        # prev | next pill tucked behind the orb
        self._gloss_button(cr, ox - 62, cy - 11, 124, 22, 11, pressed=pressed in ("prev", "next"))
        cr.set_source_rgba(0.55, 0.82, 1.0, 1)
        for d, bxx in ((-1, ox - 44), (1, ox + 44)):
            for k in (0, 1):
                tip = bxx + d * (k * 6 + 4)
                cr.move_to(tip - d * 7 + d * k * 0, cy - 5)
                cr.line_to(tip - d * 7, cy + 5)
                cr.line_to(tip, cy)
                cr.close_path()
                cr.fill()
            cr.rectangle(bxx + d * 11 - (1 if d > 0 else 1), cy - 5, 2, 10)
            cr.fill()
        # orb: glossy blue
        orb_r = 17
        cr.arc(ox, cy, orb_r + 4, 0, 2 * math.pi)
        cr.set_source_rgba(0.3, 0.65, 1.0, 0.18)
        cr.fill()
        cr.arc(ox, cy, orb_r, 0, 2 * math.pi)
        g = cairo.RadialGradient(ox, cy + orb_r * 0.55, 1, ox, cy, orb_r)
        g.add_color_stop_rgb(0, 0.45, 0.85, 1.0)
        g.add_color_stop_rgb(0.6, 0.10, 0.42, 0.85)
        g.add_color_stop_rgb(1, 0.02, 0.16, 0.42)
        cr.set_source(g)
        cr.fill_preserve()
        cr.set_source_rgba(0, 0.05, 0.15, 0.9)
        cr.set_line_width(1.2)
        cr.stroke()
        cr.save()
        cr.translate(ox, cy - orb_r * 0.45)
        cr.scale(orb_r * 0.78, orb_r * 0.45)
        cr.arc(0, 0, 1, 0, 2 * math.pi)
        cr.restore()
        g = cairo.LinearGradient(0, cy - orb_r, 0, cy)
        g.add_color_stop_rgba(0, 1, 1, 1, 0.75)
        g.add_color_stop_rgba(1, 1, 1, 1, 0.05)
        cr.set_source(g)
        cr.fill()
        cr.set_source_rgb(1, 1, 1)
        if pressed == "toggle":
            cr.arc(ox, cy, orb_r, 0, 2 * math.pi)
            cr.set_source_rgba(0.6, 0.9, 1.0, 0.35)
            cr.fill()
            cr.set_source_rgb(1, 1, 1)
        if playing(card):  # in progress: show pause bars
            cr.rectangle(ox - 5, cy - 6, 3.5, 12)
            cr.rectangle(ox + 1.5, cy - 6, 3.5, 12)
        else:
            cr.move_to(ox - 4, cy - 7)
            cr.line_to(ox - 4, cy + 7)
            cr.line_to(ox + 7, cy)
            cr.close_path()
        cr.fill()
        # where the volume knob was: playback speed readout + slider
        G = self._geom(x, y, w, h)
        vx, vs0, vs1 = G["vx"], G["vs0"], G["vs1"]
        sl = layout(cr, f"{card.speed:g}×", self.font, 11, bold=True)
        self._label(cr, sl, vx - 4, cy - lsize(sl)[1] / 2, (0.85, 0.92, 1.0, 0.95), liquid)
        if vs1 > vs0 + 10:
            k = speed_to_frac(card.speed)
            cr.rectangle(vs0, cy - 1, vs1 - vs0, 2)
            cr.set_source_rgba(0, 0, 0, 0.5)
            cr.fill()
            cr.rectangle(vs0, cy - 1, (vs1 - vs0) * k, 2)
            cr.set_source_rgba(0.55, 0.82, 1.0, 0.9)
            cr.fill()
            cr.arc(vs0 + (vs1 - vs0) * k, cy, 4.5 if pressed == "speed" else 3.5, 0, 2 * math.pi)
            cr.set_source_rgb(0.9, 0.95, 1.0)
            cr.fill()

    def box_css(self):
        return """
window.flippy-box .flippy-card { border-radius: 9px; padding: 10px 12px; color: #f4f6f7;
  background: linear-gradient(rgba(96,104,106,0.55), rgba(48,54,56,0.62) 45%, rgba(18,22,23,0.72));
  border: 1px solid rgba(0,0,0,0.7); box-shadow: inset 0 1px rgba(255,255,255,0.30), inset 0 0 0 1px rgba(255,255,255,0.10); }
window.flippy-box entry { border-radius: 12px; color: #ffffff; caret-color: #ffffff; font-size: 15px;
  text-shadow: 0 1px 2px rgba(0,0,0,0.8);
  background: linear-gradient(rgba(0,0,0,0.38), rgba(0,0,0,0.24)); border: 1px solid rgba(0,0,0,0.6);
  box-shadow: inset 0 1px 2px rgba(0,0,0,0.6), 0 1px rgba(255,255,255,0.15); }
window.flippy-box entry:focus-within { outline: none; border-color: rgba(0,0,0,0.6);
  box-shadow: inset 0 1px 2px rgba(0,0,0,0.6), 0 0 5px rgba(255,255,255,0.12); }
window.flippy-box .flippy-hint { color: rgba(235,238,240,0.7); font-size: 12px; text-shadow: 0 1px 2px rgba(0,0,0,0.8); }"""


# ---- Glass (the iOS lock screen's now-playing card in Liquid Glass) ------------

class Glass(Theme):
    """A rounded glass card: bold title, the answer, a thin progress bar, bare white transport glyphs,
    and round glass buttons for speed and close. On macOS it floats on real Liquid Glass."""
    key, name = "glass", "Glass"
    pointer = "glass"
    accent = (0.92, 0.94, 1.0)
    tap = (1.0, 1.0, 1.0)
    pen = (0.35, 0.72, 1.0)
    placeholder = "Ask about your screen"
    font = "Noto Sans"
    R, PAD, BAR_H, CTRL_H, BTN = 24, 18, 14, 46, 34
    backdrop = {"radius": R, "frost": 0.06, "smoke": 0.32}

    def _w(self, card):
        return 360 if card.follow else 440

    def _title(self, card):
        return card.header or ("Something went wrong" if card.error else "Flippy")

    def _layouts(self, cr, card, opts):
        w = self._w(card) - 2 * self.PAD
        head = layout(cr, self._title(card), self.font, opts["text_size"], width=w, bold=True)
        ellipsize_end(head)
        body = layout(cr, card.text or " ", self.font, opts["text_size"] - 1, width=w)
        return head, body

    def size(self, card, opts):
        head, body = self._layouts(_measure_ctx, card, opts)
        if card.phase == "thinking":  # just the glass and the text: no title or controls yet
            return self._w(card), self.PAD + lsize(body)[1] + self.PAD
        h = self.PAD + lsize(head)[1] + 3 + lsize(body)[1] + 14 + self.BAR_H + 8 + self.CTRL_H + 12
        return self._w(card), h

    def _geom(self, x, y, w, h):
        cy = y + h - 12 - self.CTRL_H / 2
        by = cy - self.CTRL_H / 2 - 8 - self.BAR_H / 2
        return {"cy": cy, "ox": x + w / 2, "by": by, "bx0": x + self.PAD + 46, "bx1": x + w - self.PAD - 46,
                "speed": (x + self.PAD + self.BTN / 2, cy), "close": (x + w - self.PAD - self.BTN / 2, cy)}

    def hit_regions(self, card, x, y, w, h, opts=None):
        if card.phase == "thinking":
            return {}
        G = self._geom(x, y, w, h)
        cy, ox, b = G["cy"], G["ox"], self.BTN
        hits = {"prev": (ox - 78, cy - 20, 40, 40), "toggle": (ox - 22, cy - 22, 44, 44), "next": (ox + 38, cy - 20, 40, 40),
                "seek": (G["bx0"] - 4, G["by"] - 8, G["bx1"] - G["bx0"] + 8, 16)}
        for name in ("speed", "close"):
            bx, by = G[name]
            hits["speed_cycle" if name == "speed" else name] = (bx - b / 2, by - b / 2, b, b)
        return hits

    @staticmethod
    def _glass_button(cr, cx, cy, d, pressed=False):
        """A small round glass button, like the lock screen's flashlight/camera ones."""
        r = d / 2
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.set_source_rgba(1, 1, 1, 0.22 if pressed else 0.10)
        cr.fill()
        cr.arc(cx, cy, r - 0.5, 0, 2 * math.pi)
        g = cairo.LinearGradient(0, cy - r, 0, cy + r)
        g.add_color_stop_rgba(0, 1, 1, 1, 0.55)
        g.add_color_stop_rgba(0.6, 1, 1, 1, 0.12)
        g.add_color_stop_rgba(1, 1, 1, 1, 0.3)
        cr.set_source(g)
        cr.set_line_width(1)
        cr.stroke()
        cr.arc(cx, cy, r * 0.72, math.radians(205), math.radians(250))  # glint
        cr.set_source_rgba(1, 1, 1, 0.5)
        cr.set_line_width(1.4)
        cr.stroke()

    def draw(self, cr, x, y, w, h, card, t, opts):
        a = opts["card_opacity"]
        R, P = self.R, self.PAD
        pressed = opts.get("pressed")
        if not opts.get("backdrop"):  # no real glass here: a dark, see-through card stands in
            round_rect(cr, x, y, w, h, R)
            g = cairo.LinearGradient(0, y, 0, y + h)
            g.add_color_stop_rgba(0, 0.22, 0.23, 0.26, 0.78 * a)
            g.add_color_stop_rgba(1, 0.10, 0.10, 0.12, 0.86 * a)
            cr.set_source(g)
            cr.fill()
        # the glass edge: a thin bright line, brightest along the top
        round_rect(cr, x + 0.5, y + 0.5, w - 1, h - 1, R)
        g = cairo.LinearGradient(0, y, 0, y + h)
        g.add_color_stop_rgba(0, 1, 1, 1, 0.55)
        g.add_color_stop_rgba(0.3, 1, 1, 1, 0.16)
        g.add_color_stop_rgba(1, 1, 1, 1, 0.24)
        cr.set_source(g)
        cr.set_line_width(1)
        cr.stroke()

        head, body = self._layouts(cr, card, opts)
        if card.phase == "thinking":
            word = layout(cr, thinking_word(t), self.font, opts["text_size"] - 1) if thinking(card) else body
            show(cr, word, x + P, y + P, (1, 1, 1, 0.9))
            cr.save()
            round_rect(cr, x + 1, y + 1, w - 2, h - 2, self.R)
            cr.clip()
            sweep(cr, x + P, y + h - 5, w - 2 * P, 2, t, (1, 1, 1, 0.8), radius=1)
            cr.restore()
            return
        ty = y + P
        show(cr, head, x + P, ty, (1.0, 0.6, 0.56, 1) if card.error else (1, 1, 1, 0.96))
        ty += lsize(head)[1] + 3
        show(cr, body, x + P, ty, (1, 1, 1, 0.72))

        G = self._geom(x, y, w, h)
        n = max(len(card.steps), 1)
        # progress: step counter, thin bar, steps left
        by = G["by"]
        left = f"{card.step + 1} of {n}"
        right = "done" if card.finished else f"{n - card.step - 1} left"
        for text, rx, align in ((left, x + P, 0), (right, x + w - P, 1)):
            if text:
                lay = layout(cr, text, self.font, 11)
                lw, lh = lsize(lay)
                show(cr, lay, rx - lw * align, by - lh / 2, (1, 1, 1, 0.6))
        bx0, bx1 = G["bx0"], G["bx1"]
        round_rect(cr, bx0, by - 2, bx1 - bx0, 4, 2)
        cr.set_source_rgba(1, 1, 1, 0.22)
        cr.fill()
        prog = max(0.0, min(card.progress, 1.0))
        if prog > 0:
            round_rect(cr, bx0, by - 2, max((bx1 - bx0) * prog, 4), 4, 2)
            cr.set_source_rgba(1, 1, 1, 0.92)
            cr.fill()

        # transport: bare white glyphs, like the lock screen
        cy, ox = G["cy"], G["ox"]
        for name, gx, sz in (("prev", ox - 58, 8), ("next", ox + 58, 8)):
            glyph(cr, name, gx, cy, sz, (1, 1, 1, 1.0 if pressed == name else 0.85))
        glyph(cr, "pause" if playing(card) else "play", ox, cy, 12, (1, 1, 1, 1.0 if pressed != "toggle" else 0.7))
        # round glass buttons: speed (left), close (right)
        sx, sy = G["speed"]
        self._glass_button(cr, sx, sy, self.BTN, pressed == "speed_cycle")
        sl = layout(cr, f"{card.speed:g}×", self.font, 11, bold=True)
        sw, sh = lsize(sl)
        show(cr, sl, sx - sw / 2, sy - sh / 2, (1, 1, 1, 0.92))
        cx_, cy_ = G["close"]
        self._glass_button(cr, cx_, cy_, self.BTN, pressed == "close")
        glyph(cr, "close", cx_, cy_, 5, (1, 1, 1, 0.9))

    def box_css(self):
        return """
window.flippy-box .flippy-card { background: rgba(40,40,46,0.82); border: 1px solid rgba(255,255,255,0.3);
  border-radius: 24px; padding: 12px 16px; color: #ffffff; }
window.flippy-box entry { font-size: 16px; border-radius: 12px; background: rgba(255,255,255,0.12); color: #fff; }
window.flippy-box .flippy-hint { color: rgba(255,255,255,0.6); font-size: 12px; }"""


class Mono(Theme):
    """Black with gray outlines and sharp corners, like the setup and settings windows; grayscale pointers.
    Playback is a seek slider and a speed slider (white fill, dark track, light pill knob) plus square buttons."""
    key, name = "mono", "Mono"
    pointer = "hand"
    hand_fill = (0.94, 0.945, 0.95)
    hand_outline = (0.36, 0.36, 0.38)
    accent = (0.94, 0.945, 0.95)
    tap = (0.85, 0.85, 0.87)
    pen = (0.92, 0.92, 0.94)
    bg = (0.0, 0.0, 0.0)
    outline = (0.50, 0.50, 0.52)
    fg = (0.94, 0.945, 0.95)
    muted = (0.62, 0.64, 0.68)
    err = (1.0, 0.55, 0.5)
    font = "Noto Sans"
    radius = 0
    P, BTN, ROW = 16, 26, 30

    def _w(self, card):
        return 360 if card.follow else 480

    def _layouts(self, cr, card, opts):
        w = self._w(card) - 2 * self.P
        head = layout(cr, card.header, self.font, opts["text_size"] - 1, width=w, bold=True) if card.header else None
        body = layout(cr, card.text or " ", self.font, opts["text_size"], width=w)
        return head, body

    def size(self, card, opts):
        head, body = self._layouts(_measure_ctx, card, opts)
        h = self.P + (lsize(head)[1] + 4 if head else 0) + lsize(body)[1] + self.P
        if lean_controls(card, opts):
            h += 2 * self.ROW + 6
        return self._w(card), h

    def _geom(self, x, y, w, h):
        P, B = self.P, self.BTN
        r2 = y + h - P / 2 - self.ROW / 2 - 2   # buttons and speed
        r1 = r2 - self.ROW                      # seek
        close_x = x + w - P - B
        return {"r1": r1, "r2": r2, "count": (x + P, r1),
                "seek": (x + P + 52, r1, w - 2 * P - 52),
                "prev": (x + P, r2 - B / 2), "toggle": (x + P + B + 6, r2 - B / 2),
                "next": (x + P + 2 * (B + 6), r2 - B / 2), "close": (close_x, r2 - B / 2),
                "speed": (close_x - 14 - 110, r2, 110),
                "speed_cycle": (close_x - 14 - 110 - 8 - 46, r2 - B / 2, 46, B)}  # the "1.25×" button

    def hit_regions(self, card, x, y, w, h, opts=None):
        if not lean_controls(card, opts or {}):
            return {}
        G, B = self._geom(x, y, w, h), self.BTN
        hits = {name: (*G[name], B, B) for name in ("prev", "toggle", "next", "close")}
        sx, sy, sw = G["seek"]
        hits["seek"] = (sx, sy - 9, sw, 18)
        vx, vy, vw = G["speed"]
        hits["speed"] = (vx, vy - 9, vw, 18)
        hits["speed_cycle"] = G["speed_cycle"]
        return hits

    def _slider(self, cr, x, cy, w, frac, pressed):
        """A sharp bar: white fill up to the value, dark track after it. The knob shows only while it's held."""
        frac = max(0.0, min(frac, 1.0))
        cr.rectangle(x, cy - 3, w, 6)
        cr.set_source_rgba(0.16, 0.16, 0.17, 1)
        cr.fill()
        cr.rectangle(x, cy - 3, w * frac, 6)
        cr.set_source_rgba(*self.fg, 1)
        cr.fill()
        if pressed:
            kw, kh = 22, 14
            kx = x + (w - kw) * frac
            round_rect(cr, kx, cy - kh / 2 + 1, kw, kh, kh / 2)  # shadow
            cr.set_source_rgba(0, 0, 0, 0.5)
            cr.fill()
            round_rect(cr, kx, cy - kh / 2, kw, kh, kh / 2)
            cr.set_source_rgba(0.85, 0.85, 0.86, 1)
            cr.fill()

    def _button(self, cr, bx, by, name, pressed):
        B = self.BTN
        if pressed:
            cr.rectangle(bx, by, B, B)
            cr.set_source_rgba(0.16, 0.16, 0.17, 1)
            cr.fill()
        cr.rectangle(bx + 0.5, by + 0.5, B - 1, B - 1)
        cr.set_source_rgba(*self.outline, 1)
        cr.set_line_width(1)
        cr.stroke()
        glyph(cr, name, bx + B / 2, by + B / 2, 5 if name != "close" else 4.5, (*self.fg, 1))

    def draw(self, cr, x, y, w, h, card, t, opts):
        cr.rectangle(x + 0.5, y + 0.5, w - 1, h - 1)
        cr.set_source_rgba(*self.bg, opts["card_opacity"])
        cr.fill_preserve()
        cr.set_source_rgba(*self.outline, 1)
        cr.set_line_width(1)
        cr.stroke()
        head, body = self._layouts(cr, card, opts)
        ty = y + self.P
        if head:
            show(cr, head, x + self.P, ty, (*self.fg, 1))
            ty += lsize(head)[1] + 4
        if thinking(card):  # the dots cycle, and a bar sweeps along the bottom edge
            show(cr, layout(cr, thinking_word(t), self.font, opts["text_size"]), x + self.P, ty, (*self.fg, 1))
            sweep(cr, x + 1, y + h - 3, w - 2, 2, t, (*self.fg, 0.9))
            return
        show(cr, body, x + self.P, ty, (*(self.err if card.error else (self.fg if head is None else self.muted)), 1))
        if not lean_controls(card, opts):
            return
        G = self._geom(x, y, w, h)
        pressed = opts.get("pressed")
        cr.rectangle(x + self.P, G["r1"] - self.ROW / 2 - 2, w - 2 * self.P, 1)  # divider above the controls
        cr.set_source_rgba(0.22, 0.22, 0.23, 1)
        cr.fill()
        n = max(len(card.steps), 1)
        count = layout(cr, f"{min(card.step + 1, n)} / {n}", self.font, 11)
        cx, cy = G["count"]
        show(cr, count, cx, cy - lsize(count)[1] / 2, (*self.muted, 1))
        sx, sy, sw = G["seek"]
        self._slider(cr, sx, sy, sw, card.progress, pressed == "seek")
        for name in ("prev", "toggle", "next", "close"):
            g = name if name != "toggle" else ("pause" if playing(card) else "play")
            self._button(cr, *G[name], g, pressed == name)
        vx, vy, vw = G["speed"]
        bx, by, bw, bh = G["speed_cycle"]  # a square-cornered button: click steps through the speeds
        if pressed == "speed_cycle":
            cr.rectangle(bx, by, bw, bh)
            cr.set_source_rgba(0.16, 0.16, 0.17, 1)
            cr.fill()
        cr.rectangle(bx + 0.5, by + 0.5, bw - 1, bh - 1)
        cr.set_source_rgba(*self.outline, 1)
        cr.set_line_width(1)
        cr.stroke()
        sl = layout(cr, f"{card.speed:g}×", self.font, 11, bold=True)
        show(cr, sl, bx + (bw - lsize(sl)[0]) / 2, by + (bh - lsize(sl)[1]) / 2, (*self.fg, 1))
        self._slider(cr, vx, vy, vw, speed_to_frac(card.speed), pressed == "speed")

    def box_css(self):
        return """
window.flippy-box .flippy-card { background: rgba(0,0,0,0.96); border: 1px solid rgb(128,128,133);
  border-radius: 0; padding: 12px 16px; color: #f0f1f2; }
window.flippy-box entry { font-size: 16px; border-radius: 0; background: #000; color: #f0f1f2;
  border: 1px solid rgb(128,128,133); }
window.flippy-box .flippy-hint { color: rgba(240,241,242,0.55); font-size: 12px; }"""


THEMES = {th.key: th for th in (Mono(), Midnight(), Y2K(), MediaPlayer(), Glass(), Terminal(), Cosmic())}


def get(key):
    return THEMES.get(key, THEMES["mono"])
