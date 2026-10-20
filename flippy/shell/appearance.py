"""The Appearance page's Python side: gallery images, the pointer studio's load and save, and import/export.

The themes draw their own gallery tiles (the same code that draws them on screen), so the gallery can't drift.
Pointers keep their files and index in flippy/pointers.py; the studio in the page only ever sends pixels, a
smooth canvas or an image, plus the tip, and this module saves them the way the old native editors did.
"""
import base64
import io
import json
import re

import cairo
from PIL import Image, PngImagePlugin

from .. import pixelart, pointers, settings, themes
from .setup import data_url

TILE_W, TILE_H, SCALE = 288, 150, 2         # theme tiles, CSS px; rendered at 2x for sharp screens
POINTER_TILE, POINTER_SCALE = 72, 2
BUILTIN_POINTERS = ["hand", "arrow", "ring", "dot", "glass", "glasshand"]
THEME_NAMES = {"cosmic": "System"}           # the gallery's names where they differ from the theme's own
PNG_KEY = "flippy-pointer"                   # the PNG text chunk an exported pointer carries its tip in
LOOK_FORMAT = "flippy-look/1"
LOOK_KEYS = ["theme", "pointer", "pointer_size", "text_size", "card_opacity", "glass_tint", "glass_color",
             "controls", "player_shine", "frosted"]


def _png_url(surf):
    buf = io.BytesIO()
    surf.write_to_png(buf)
    return data_url(buf.getvalue())


def _surface_from_png(data):
    return cairo.ImageSurface.create_from_png(io.BytesIO(data))


def _from_data_url(url, limit=4 * 1024 * 1024):
    m = re.fullmatch(r"data:image/png;base64,([A-Za-z0-9+/=]+)", url or "")
    if not m or len(m.group(1)) > limit:
        raise ValueError("not a PNG")
    return base64.b64decode(m.group(1))


# ---- gallery
def _backdrop(cr, w, h):
    """The tiles' backdrop: the same dusk gradient the old settings thumbnails used."""
    g = cairo.LinearGradient(0, 0, w, h)
    g.add_color_stop_rgb(0, 0.33, 0.2, 0.3)
    g.add_color_stop_rgb(1, 0.13, 0.11, 0.24)
    cr.set_source(g)
    cr.paint()


def theme_tile(key):
    theme = themes.get(key)
    w, h = TILE_W * SCALE, TILE_H * SCALE
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(surf)
    cr.scale(SCALE, SCALE)
    _backdrop(cr, TILE_W, TILE_H)
    sample = themes.Card(text="The pointer glides here and the panel follows it.", header="Wi-Fi",
                         steps=["Control Center", "Wi-Fi"], step=1, progress=0.45, meta="OPUS · LOW", follow=True)
    opts = {"text_size": 15, "card_opacity": settings.get("look", "card_opacity"),
            "controls": settings.get("look", "controls"), "player_shine": settings.get("look", "player_shine")}
    cw, ch = theme.size(sample, opts)
    k = min((TILE_W - 56) / cw, (TILE_H - 20) / ch, 1.0)
    cr.save()
    cr.translate(46, (TILE_H - ch * k) / 2)
    cr.scale(k, k)
    theme.draw(cr, 0, 0, cw, ch, sample, 1.2, opts)
    cr.restore()
    themes.draw_pointer(cr, theme, theme.pointer, 24, 20, 1.0, 0.45, 10_000)
    return _png_url(surf)


def themes_gallery():
    return [{"key": k, "name": THEME_NAMES.get(k, t.name), "pointer": t.pointer, "image": theme_tile(k)}
            for k, t in themes.THEMES.items()]


def pointer_tile(style, theme_key=None):
    theme = themes.get(theme_key or settings.get("look", "theme"))
    size = POINTER_TILE * POINTER_SCALE
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surf)
    cr.scale(POINTER_SCALE, POINTER_SCALE)
    name = theme.pointer if style == "theme" else style
    if name.startswith(pointers.PREFIX):
        r, left, below, above = pointers.extent(name[len(pointers.PREFIX):], 1.0, themes.sprite_px(0.9))
    else:
        r, left, below, above = 22, 4, 26, 4
    x = (POINTER_TILE - (r + left)) / 2 + left
    y = (POINTER_TILE - (below + above)) / 2 + above
    themes.draw_pointer(cr, theme, name, x, y, 1.0, 0.9, 10_000)
    return _png_url(surf)


def pointers_gallery():
    out = [{"value": "theme", "name": "Theme default", "custom": False, "image": pointer_tile("theme")}]
    labels = dict(settings.options("look", "pointer"))
    out += [{"value": p, "name": labels.get(p, p.capitalize()), "custom": False, "image": pointer_tile(p)}
            for p in BUILTIN_POINTERS]
    out += [{"value": pointers.PREFIX + n, "name": n, "custom": True, "image": pointer_tile(pointers.PREFIX + n)}
            for n in pointers.names()]
    return out


# ---- the studio
def _hex(rgb):
    return None if rgb is None else pixelart.to_hex(rgb)


def _studio_base():
    """What every studio opening carries: the grid, the smooth canvas's resolution, the palette and brushes."""
    return {"grid": [pixelart.GRID_W, pixelart.GRID_H], "res": pixelart.RES,
            "palette": [_hex(c) for c in pixelart.PALETTE], "brushes": list(pixelart.BRUSHES)}


def _existing(name):
    if name not in pointers.names():
        raise ValueError(f"no pointer {name}")


def studio_open(name=None, start=None):
    """What the studio shows: a new drawing, a template (hand, arrow) or an existing pointer."""
    meta = pointers.meta(name) if name else None
    surf = pointers.surface(name) if name else None
    if name and surf is None:
        raise ValueError(f"no pointer {name}")
    image_mode = bool(name) and not ((meta["pixel"] and surf.get_width() <= pixelart.GRID_W
                                      and surf.get_height() <= pixelart.GRID_H) or meta["grid_h"])
    out = dict(_studio_base(), name=name or pointers.unique_name("My pointer"), editing=name,
               flip=meta["flip"] if meta else True)
    if image_mode:
        return dict(out, mode="image", image=_png_url(surf), size=[surf.get_width(), surf.get_height()],
                    hotspot=list(meta["hotspot"]), crisp=meta["pixel"])
    art = pixelart.PixelArt(name)
    if start in ("hand", "arrow"):
        art.start(start)
    if art.pixel:
        return dict(out, mode="pixel", cells=[[_hex(c) for c in row] for row in art.cells], hotspot=list(art.hotspot))
    return dict(out, mode="smooth", image=_png_url(art.smooth), hotspot=list(art.hotspot))


def template(kind):
    art = pixelart.PixelArt()
    art.start(kind)
    return {"cells": [[_hex(c) for c in row] for row in art.cells], "hotspot": list(art.hotspot)}


def _cells(rows):
    gw, gh = pixelart.GRID_W, pixelart.GRID_H
    if not isinstance(rows, list) or len(rows) != gh or any(not isinstance(r, list) or len(r) != gw for r in rows):
        raise ValueError("bad grid")
    out = []
    for row in rows:
        line = []
        for c in row:
            rgb = None if c is None else pixelart.from_hex(str(c))
            if c is not None and rgb is None:
                raise ValueError("bad color")
            line.append(rgb)
        out.append(line)
    return out


def _cell(hotspot, w, h):
    if (not isinstance(hotspot, list) or len(hotspot) != 2 or not all(type(v) is int for v in hotspot)
            or not (0 <= hotspot[0] < w and 0 <= hotspot[1] < h)):
        raise ValueError("bad tip")
    return tuple(hotspot)


def _name(name):
    name = re.sub(r"[^a-zA-Z0-9 _-]", "", str(name or "")).strip()[:40]
    return name or "My pointer"


def studio_save(mode, name, hotspot, flip=True, editing=None, cells=None, image=None, crisp=False):
    """Save what the studio made; returns the pointer setting value ("custom:<name>")."""
    name = _name(name)
    if editing is not None and editing not in pointers.names():
        editing = None
    if mode == "pixel":
        art = pixelart.PixelArt()
        art.cells = _cells(cells)
        art.hotspot = _cell(hotspot, pixelart.GRID_W, pixelart.GRID_H)
        name = art.save(name, editing, bool(flip))
    elif mode == "smooth":
        surf = _surface_from_png(_from_data_url(image))
        if (surf.get_width(), surf.get_height()) != (pixelart.GRID_W * pixelart.RES, pixelart.GRID_H * pixelart.RES):
            raise ValueError("bad canvas size")
        art = pixelart.PixelArt()
        art.smooth = surf
        art.hotspot = _cell(hotspot, pixelart.GRID_W, pixelart.GRID_H)
        name = art.save(name, editing, bool(flip))
    elif mode == "image":
        surf = _surface_from_png(_from_data_url(image))
        if max(surf.get_width(), surf.get_height()) > pointers.MAX_IMPORT_PX:
            raise ValueError("image too large")
        tip = _cell(hotspot, surf.get_width(), surf.get_height())
        if editing and name != editing:
            pointers.delete(editing)
        pointers.save(name, surf, tip, pixel=bool(crisp), flip=bool(flip))
    else:
        raise ValueError("bad mode")
    return pointers.PREFIX + name


def _copy(old, new):
    m = pointers.meta(old)
    pointers.save(new, pointers.surface(old), m["hotspot"], m["pixel"], m["flip"], m["grid_h"])


def rename(old, new):
    new = _name(new)
    _existing(old)
    if new == old:
        return pointers.PREFIX + old
    new = pointers.unique_name(new)
    _copy(old, new)
    pointers.delete(old)
    if settings.get("look", "pointer") == pointers.PREFIX + old:
        settings.set("look", "pointer", pointers.PREFIX + new)
    return pointers.PREFIX + new


def duplicate(name):
    _existing(name)
    new = pointers.unique_name(name + " copy")
    _copy(name, new)
    return pointers.PREFIX + new


def delete(name):
    _existing(name)
    if settings.get("look", "pointer") == pointers.PREFIX + name:
        settings.set("look", "pointer", "theme")
    pointers.delete(name)


# ---- pointer files: a PNG that carries its own tip
def export_png(name):
    """The pointer as PNG bytes, with its tip and style in a text chunk, so importing it needs no picking."""
    _existing(name)
    buf = io.BytesIO()
    pointers.surface(name).write_to_png(buf)
    info = PngImagePlugin.PngInfo()
    info.add_text(PNG_KEY, json.dumps(pointers.meta(name)))
    out = io.BytesIO()
    Image.open(io.BytesIO(buf.getvalue())).save(out, "PNG", pnginfo=info)
    return out.getvalue()


def import_file(data, filename="Image pointer"):
    """A pointer PNG exported from Flippy is saved as is; any other image opens in the studio to pick its tip."""
    im = Image.open(io.BytesIO(data))
    meta = None
    raw = im.info.get(PNG_KEY) if hasattr(im, "info") else None
    if raw:
        try:
            meta = json.loads(raw)
        except ValueError:
            meta = None
    base = re.sub(r"\.[A-Za-z0-9]+$", "", filename)
    if isinstance(meta, dict) and isinstance(meta.get("hotspot"), list) and max(im.size) <= 512:
        surf = _surface_from_png(_png_bytes(im.convert("RGBA")))
        name = pointers.unique_name(_name(base))
        grid_h = meta.get("grid_h") if type(meta.get("grid_h")) is int else None
        tip = _cell([int(v) for v in meta["hotspot"]], surf.get_width(), surf.get_height())
        pointers.save(name, surf, tip, bool(meta.get("pixel", True)), bool(meta.get("flip", True)), grid_h)
        return {"saved": pointers.PREFIX + name}
    im = im.convert("RGBA")
    im.thumbnail((pointers.MAX_IMPORT_PX, pointers.MAX_IMPORT_PX), Image.LANCZOS)
    w, h = im.size
    return {"studio": dict(_studio_base(), mode="image", name=pointers.unique_name(_name(base)), editing=None,
                           flip=True, image=data_url(_png_bytes(im)), size=[w, h], hotspot=[w // 2, 0],
                           crisp=max(w, h) <= 48)}


def _png_bytes(im):
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


# ---- looks: a small JSON anyone can copy, paste and keep
def export_look():
    look = {"format": LOOK_FORMAT}
    look.update({k: settings.get("look", k) for k in LOOK_KEYS})
    p = look["pointer"]
    if isinstance(p, str) and p.startswith(pointers.PREFIX) and p[len(pointers.PREFIX):] in pointers.names():
        name = p[len(pointers.PREFIX):]
        look["pointer"] = {"name": name, "png": base64.b64encode(export_png(name)).decode()}
    return json.dumps(look, indent=2)


def read_look(text):
    """Parse a look; returns (changes as [(key, value)], pointer png or None, skipped keys)."""
    try:
        look = json.loads(text)
    except ValueError as e:
        raise ValueError("That isn't a Flippy look (not JSON).") from e
    if not isinstance(look, dict) or look.get("format") != LOOK_FORMAT:
        raise ValueError("That isn't a Flippy look (no \"format\": \"flippy-look/1\").")
    changes, skipped, png = [], [], None
    for k in LOOK_KEYS:
        if k not in look:
            continue
        v = look[k]
        if k == "pointer" and isinstance(v, dict):
            try:
                png = (str(v.get("name") or "Shared pointer"), base64.b64decode(str(v.get("png", "")), validate=True))
            except ValueError:
                skipped.append(k)
            continue
        if settings._valid("look", k, v):
            changes.append((k, v))
        else:
            skipped.append(k)
    skipped += [k for k in look if k not in LOOK_KEYS and k != "format"]
    return changes, png, skipped


def apply_look(text):
    changes, png, skipped = read_look(text)
    before = {k: settings.get("look", k) for k in LOOK_KEYS}
    for k, v in changes:
        settings.set("look", k, v)
    if png:
        name, data = png
        saved = import_file(data, name).get("saved")
        if saved:
            settings.set("look", "pointer", saved)
        else:
            skipped.append("pointer")
    return {"before": before, "skipped": skipped}
