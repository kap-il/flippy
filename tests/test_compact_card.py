"""Answers in a card as narrow as the question box (#37): every theme draws at that width with its controls inside and
apart, and the pointer glides out from the mouse rather than dropping in where it points."""
import unittest
from unittest.mock import patch

import cairo

from flippy import overlay, placement, settings, themes

W, H = 1512, 982
NARROW = 191  # 1.5" on a 14" MacBook
SHORT = "Click Save."
LONG = " ".join(["This answer goes on for a while so it needs a lot of room on the screen."] * 4)


class Overlay(overlay.OverlayBase):
    card_w = NARROW
    at = None  # where the mouse is

    def queue_draw(self): pass
    def set_opacity(self, a): pass
    def start_ticking(self): pass
    def stop_ticking(self): pass
    def set_drawing_input(self, on): pass
    def card_width(self): return self.card_w
    def mouse(self): return self.at


class Base(unittest.TestCase):
    def setUp(self):
        saver = patch.object(settings, "_save")
        saver.start()
        self.addCleanup(saver.stop)
        for key, value in (("theme", "mono"), ("card_place", "auto"), ("controls", "all")):
            old = settings.get("look", key)
            self.addCleanup(settings.set, "look", key, old)
            settings.set("look", key, value)
        self.ov = Overlay()


class Card(Base):
    def paint(self):
        self.ov.paint(cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)), W, H)
        return self.ov.hits

    def test_every_theme_is_the_box_width_with_its_controls_inside_and_apart(self):
        for key in themes.THEMES:
            for text, header, steps in ((SHORT, "Save", ["Save"]), (LONG, None, []), (LONG, "Save", ["a", "b", "c"])):
                with self.subTest(theme=key, text=text[:12], steps=len(steps)):
                    settings.set("look", "theme", key)
                    self.ov.clear()
                    self.ov.point(400, 300, "Save")
                    self.ov.show_text(text, header=header, follow=True, controls=True, steps=steps)
                    hits = self.paint()
                    (x, y, w, h), _ = self.ov.card_layout(W, H)
                    self.assertEqual(w, NARROW)
                    self.assertTrue(hits)
                    for name, (hx, hy, hw, hh) in hits.items():
                        self.assertTrue(x <= hx and hx + hw <= x + w and y <= hy and hy + hh <= y + h, name)
                    names = sorted(hits)
                    for i, a in enumerate(names):
                        for b in names[i + 1:]:
                            # Media Player's big play button sits on its seek bar, as at its own width
                            if not (key == "mediaplayer" and {a, b} == {"seek", "toggle"}):
                                self.assertEqual(placement.overlap(hits[a], hits[b]), 0, f"{a} / {b}")

    def test_elsewhere_each_theme_keeps_its_own_width(self):
        self.ov.card_w = None  # COSMIC: no card_w
        for key in themes.THEMES:
            with self.subTest(theme=key):
                settings.set("look", "theme", key)
                self.ov.clear()
                self.ov.show_text(LONG, controls=True)
                (_, _, w, _), _ = self.ov.card_layout(W, H)
                self.assertGreater(w, 300)


class Glide(Base):
    def test_the_first_point_glides_out_from_the_mouse(self):
        self.ov.at = (700, 500)
        self.ov.point(20, 10, "Apple menu")
        self.assertEqual(self.ov.pos, (700, 500))
        self.assertEqual(self.ov.move_from, (700, 500))
        self.assertEqual(self.ov.target[:2], (20, 10))

    def test_it_drops_in_where_the_mouse_is_unknown(self):
        self.ov.point(20, 10, "Apple menu")
        self.assertEqual(self.ov.pos, (20, 10))
        self.assertIsNone(self.ov.move_from)

    def test_later_points_glide_from_where_it_is(self):
        self.ov.at = (700, 500)
        self.ov.point(20, 10, "Apple menu")
        self.ov.pos, self.ov.move_from = (20, 10), None  # arrived
        self.ov.point(1400, 10, "Clock")
        self.assertEqual(self.ov.move_from, (20, 10))


if __name__ == "__main__":
    unittest.main()
