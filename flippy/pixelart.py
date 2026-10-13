"""The pointer studio's model on the Python side (flippy/shell/appearance.py): the grid, load and save. The
drawing itself happens in the page (flippy/shell/web/studio.js).

Two ways to draw: pixel art on the GRID_W x GRID_H grid (cells), or smooth (a normal drawing: round brush
strokes on a canvas RES times finer than the grid). Either way the tip (hotspot) is a grid cell, and the
pointer shows at the same size.
"""
import re

import cairo

from . import pointers, themes

GRID_W, GRID_H = 20, 24
RES = 16                    # smooth drawing: canvas pixels per grid cell
BRUSHES = (6, 12, 24)       # smooth brush widths, in canvas pixels (thin, medium, thick)
PALETTE = [None, (0, 0, 0), (1, 1, 1), (0.55, 0.55, 0.6), (1.0, 0.25, 0.2), (1.0, 0.6, 0.1), (1.0, 0.9, 0.2),
           (0.2, 0.85, 0.35), (0.25, 0.6, 1.0), (0.6, 0.35, 0.95), (1.0, 0.5, 0.75), (0.55, 0.35, 0.2)]


def to_hex(rgb):
    return "#" + "".join(f"{round(max(0, min(c, 1)) * 255):02X}" for c in rgb)


def from_hex(text):
    """'#RGB', '#RRGGBB' or without the # -> (r, g, b) 0-1, else None."""
    text = text.strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{3}", text):
        text = "".join(c * 2 for c in text)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", text):
        return None
    return tuple(int(text[i:i + 2], 16) / 255 for i in (0, 2, 4))


def from_sprite(grid_rows, tip_col):
    """Built-in sprite (# outline . fill) -> centered grid + hotspot."""
    cells = [[None] * GRID_W for _ in range(GRID_H)]
    ox = (GRID_W - len(grid_rows[0])) // 2
    oy = (GRID_H - len(grid_rows)) // 2
    for r, row in enumerate(grid_rows):
        for c, ch in enumerate(row):
            if ch != " ":
                cells[oy + r][ox + c] = (0, 0, 0) if ch == "#" else (1, 1, 1)
    return cells, (ox + tip_col, oy)


def _blank_smooth():
    return cairo.ImageSurface(cairo.FORMAT_ARGB32, GRID_W * RES, GRID_H * RES)


def _copy(surf):
    out = _blank_smooth()
    cr = cairo.Context(out)
    cr.set_source_surface(surf)
    cr.paint()
    return out


class PixelArt:
    def __init__(self, name=None):
        self.cells = [[None] * GRID_W for _ in range(GRID_H)]
        self.smooth = None        # a cairo surface when drawing smooth; None = pixel art
        self.hotspot = (GRID_W // 2, 0)
        if name:
            self.load(name)

    @property
    def pixel(self):
        return self.smooth is None

    def _cells_to_smooth(self):
        surf = _blank_smooth()
        cr = cairo.Context(surf)
        cr.set_antialias(cairo.ANTIALIAS_NONE)
        for y, row in enumerate(self.cells):
            for x, c in enumerate(row):
                if c is not None:
                    cr.set_source_rgb(*c)
                    cr.rectangle(x * RES, y * RES, RES, RES)
                    cr.fill()
        surf.flush()
        return surf

    def start(self, kind):
        rows, tip = (themes.HAND, themes.HAND_TIP_COL) if kind == "hand" else (themes.ARROW, 0)
        self.cells, self.hotspot = from_sprite(rows, tip)
        if not self.pixel:
            self.smooth = self._cells_to_smooth()

    def load(self, name):
        surf = pointers.surface(name)
        if surf is None:
            return
        m = pointers.meta(name)
        if not m["pixel"] and (surf.get_width(), surf.get_height()) == (GRID_W * RES, GRID_H * RES):
            self.smooth = _copy(surf)  # a smooth drawing made here
            self.hotspot = (m["hotspot"][0] // RES, m["hotspot"][1] // RES)
            return
        w, h = min(surf.get_width(), GRID_W), min(surf.get_height(), GRID_H)
        data, stride = surf.get_data(), surf.get_stride()
        for yy in range(h):
            for xx in range(w):
                b, g, r, a = data[yy * stride + xx * 4: yy * stride + xx * 4 + 4]
                if a > 127:  # premultiplied ARGB32, little-endian -> BGRA bytes
                    self.cells[yy][xx] = (r / a, g / a, b / a)
        self.hotspot = tuple(pointers.meta(name)["hotspot"])

    def surface(self):
        if not self.pixel:
            return _copy(self.smooth)
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, GRID_W, GRID_H)
        cr = cairo.Context(surf)
        for y in range(GRID_H):
            for x in range(GRID_W):
                c = self.cells[y][x]
                if c is not None:
                    cr.set_source_rgb(*c)
                    cr.rectangle(x, y, 1, 1)
                    cr.fill()
        surf.flush()
        return surf

    def _smooth_hotspot(self):
        """The tip cell's center on the smooth canvas."""
        return self.hotspot[0] * RES + RES // 2, self.hotspot[1] * RES + RES // 2

    def save(self, name, editing=None, flip=True):
        name = name.strip() or "My pointer"
        if editing and name != editing:
            pointers.delete(editing)
        if self.pixel:
            pointers.save(name, self.surface(), self.hotspot, pixel=True, flip=flip)
        else:
            pointers.save(name, self.surface(), self._smooth_hotspot(), pixel=False, flip=flip, grid_h=GRID_H)
        return name


# Scripted demo (`flippy-ask demo-pointer`): outline -> flood fill -> highlights -> sparkles -> tip.
DEMO_SPRITE = [
    "o          s   ",
    "oo           s ",
    "ofo       s    ",
    "ohfo           ",
    "ohffo          ",
    "ohfffo         ",
    "ohffffo        ",
    "ohfffffo       ",
    "ohffffffo      ",
    "ohfffffffo     ",
    "ohffffffffo    ",
    "ohfffffffffo   ",
    "ohfffffoooooo  ",
    "ohffoffo       ",
    "ohfo offo      ",
    "ofo  offo      ",
    "oo    offo     ",
    "o     offo     ",
    "       oo      ",
]
DEMO_COLORS = {"o": (0.2, 0.08, 0.38), "f": (1.0, 0.6, 0.1), "h": (1.0, 0.5, 0.75), "s": (1.0, 0.9, 0.2)}
