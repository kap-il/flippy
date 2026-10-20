"""Smooth (non-pixel) pointer drawings in the studio's model (flippy/pixelart.py): saved at the pixel grid's size
with the tip on a grid cell, and reopened smooth."""
import os
import tempfile
import unittest
from unittest.mock import patch

import cairo

from flippy import pointers
from flippy.pixelart import GRID_H, RES, PixelArt, _blank_smooth


def smooth_art(x, y, w, h):
    """A smooth drawing with one filled rectangle (smooth canvas px), as the studio's canvas would send it."""
    art = PixelArt()
    art.smooth = _blank_smooth()
    cr = cairo.Context(art.smooth)
    cr.rectangle(x, y, w, h)
    cr.fill()
    return art


class Smooth(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        for attr, value in (("DIR", d.name), ("INDEX", os.path.join(d.name, "index.json"))):
            p = patch.object(pointers, attr, value)
            p.start()
            self.addCleanup(p.stop)
        pointers._cache.clear()

    def test_saved_at_grid_size_with_the_tip_on_a_cell_and_reopens_smooth(self):
        art = smooth_art(40, 40, 30, 30)
        art.hotspot = (3, 2)
        name = art.save("Doodle")
        m = pointers.meta(name)
        self.assertFalse(m["pixel"])
        self.assertEqual(m["grid_h"], GRID_H)
        self.assertEqual(m["hotspot"], [3 * RES + RES // 2, 2 * RES + RES // 2])
        surf = pointers.surface(name)
        # shown as tall as a pixel pointer of the same grid
        self.assertAlmostEqual(pointers._scale(surf, m, 1.0, 4) * surf.get_height(), 4 * GRID_H)
        again = PixelArt(name)
        self.assertFalse(again.pixel)
        self.assertEqual(again.hotspot, (3, 2))

    def test_pixel_art_still_saves_as_pixels(self):
        art = PixelArt()
        art.start("arrow")
        m = pointers.meta(art.save("Arrowish"))
        self.assertTrue(m["pixel"])
        self.assertIsNone(m["grid_h"])


if __name__ == "__main__":
    unittest.main()
