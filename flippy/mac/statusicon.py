"""The menu bar icon: the pointer you have equipped, with click rays around its tip.

Drawn with cairo as a template image (black + alpha), so macOS tints it for a
light or dark menu bar. Pixel pointers are scaled nearest-neighbour so they stay
crisp; round pointers (dot, ring, glass lens) get rays all around.
"""
import math

import cairo
from AppKit import NSImage
from Foundation import NSData

from .. import pointers, settings, themes

W, H = 26, 22          # points
S = 2                  # @2x
RAY_W = 1.5


def _sprite_surface(rows, solid=True):
    """A pixel sprite as an alpha mask: '#' always, '.' too when solid (else hollow)."""
    w, h = max(len(r) for r in rows), len(rows)
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(surf)
    cr.set_source_rgba(0, 0, 0, 1)
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch == "#" or (solid and ch == "."):
                cr.rectangle(x, y, 1, 1)
    cr.fill()
    return surf


def _glass_hand_surface(shape):
    """The glass hand's pieces as one silhouette, tip at (pad, pad)."""
    pad = 30
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 120, 120)
    cr = cairo.Context(surf)
    cr.set_source_rgba(0, 0, 0, 1)
    for px, py, w, h, r, rot in shape:
        themes._piece_path(cr, px + pad, py + pad + 2, w, h, r, rot)
    cr.fill()
    xs = [p[0] for p in shape]
    ws = [p[0] + p[2] for p in shape]
    hs = [p[1] + p[3] for p in shape]
    x0, x1, y1 = pad + min(xs) - 4, pad + max(ws) + 2, pad + max(hs) + 4
    out = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(x1 - x0), int(y1 - pad))
    c2 = cairo.Context(out)
    c2.set_source_surface(surf, -x0, -pad)
    c2.paint()
    return out, pad - x0, 0, False   # surface, tip x, tip y, pixel art?


def _custom_surface(name):
    surf = pointers.surface(name)
    if surf is None:
        return None
    hx, hy = pointers.meta(name)["hotspot"]
    mask = cairo.ImageSurface(cairo.FORMAT_ARGB32, surf.get_width(), surf.get_height())
    cr = cairo.Context(mask)
    cr.set_source_rgba(0, 0, 0, 1)
    cr.mask_surface(surf, 0, 0)  # its shape, in black
    return mask, hx + 0.5, hy, pointers.meta(name)["pixel"]


def _rays(cr, x, y, angles, start, length):
    cr.set_line_width(RAY_W)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    for a in angles:
        r = math.radians(a)
        cr.move_to(x + start * math.cos(r), y + start * math.sin(r))
        cr.line_to(x + (start + length) * math.cos(r), y + (start + length) * math.sin(r))
    cr.stroke()


def _round(cr, style):
    """dot / ring / glass lens, with rays off its top and left (a click, not a sun)."""
    cx, cy = W / 2 + 2, H / 2 + 2
    if style == "dot":
        cr.arc(cx, cy, 3.6, 0, 2 * math.pi)
        cr.fill()
        r_out = 4.5
    elif style == "ring":
        cr.set_line_width(1.6)
        cr.arc(cx, cy, 4.6, 0, 2 * math.pi)
        cr.stroke()
        cr.arc(cx, cy, 1.4, 0, 2 * math.pi)
        cr.fill()
        r_out = 5.6
    else:  # glass lens: rim, glint, centre dot
        cr.set_line_width(1.4)
        cr.arc(cx, cy, 5.2, 0, 2 * math.pi)
        cr.stroke()
        cr.set_line_width(1.2)
        cr.arc(cx, cy, 3.4, math.radians(200), math.radians(255))
        cr.stroke()
        cr.arc(cx, cy, 1.1, 0, 2 * math.pi)
        cr.fill()
        r_out = 6.0
    _rays(cr, cx, cy, (-90, -135, 180, -45, 135), r_out + 1.2, 2.6)


def _shape(style, theme):
    if style.startswith(pointers.PREFIX):
        got = _custom_surface(style[len(pointers.PREFIX):])
        if got:
            return got
        style = theme.pointer
    if style in themes.PIXEL_HANDS:
        rows, tip = themes.PIXEL_HANDS[style]
        return _sprite_surface(rows, solid=False), tip + 0.5, 0, True
    if style == "arrow":
        return _sprite_surface(themes.ARROW), 0.5, 0, True
    if style in themes.GLASS_HANDS:
        return _glass_hand_surface(themes.GLASS_HANDS[style])
    return None  # round styles are drawn directly


def render():
    """PNG bytes (@2x) of the icon for the equipped pointer."""
    theme = themes.get(settings.get("look", "theme"))
    style = settings.get("look", "pointer")
    if style == "theme":
        style = theme.pointer
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, W * S, H * S)
    cr = cairo.Context(surf)
    cr.scale(S, S)
    cr.set_source_rgba(0, 0, 0, 1)
    shape = _shape(style, theme)
    if shape is None:
        _round(cr, style)
    else:
        mask, tx, ty, pixel = shape
        mw, mh = mask.get_width(), mask.get_height()
        corner = tx < mw * 0.25          # tip in the top-left corner (arrow) vs top-middle (hand)
        tip = (9.5, 7.0) if corner else (W / 2 + 2.5, 7.0)
        k = min((H - tip[1] - 0.5) / (mh - ty), (W - 1 - tip[0]) / max(mw - tx, 1),
                (tip[0] - 0.5) / max(tx, 0.5) if tx > 0.5 else 99)
        cr.save()
        cr.translate(tip[0] - tx * k, tip[1] - ty * k)
        cr.scale(k, k)
        pat = cairo.SurfacePattern(mask)
        pat.set_filter(cairo.FILTER_NEAREST if pixel else cairo.FILTER_GOOD)
        cr.mask(pat)
        cr.restore()
        cr.set_source_rgba(0, 0, 0, 1)
        angles = (-90, -135, 180, 135, -45) if corner else (-90, -130, -50, -165, -15)
        _rays(cr, tip[0], tip[1], angles, 2.2, 2.8)
    surf.flush()
    import io
    buf = io.BytesIO()
    surf.write_to_png(buf)
    return buf.getvalue()


def image():
    png = render()
    img = NSImage.alloc().initWithData_(NSData.dataWithBytes_length_(png, len(png)))
    img.setSize_((W, H))
    img.setTemplate_(True)  # macOS tints it for the menu bar
    return img
