"""Answers in a card as narrow as the question box (#37): every theme draws at that width with its controls inside and
apart; the pointer glides out from the mouse rather than dropping in where it points; and thinking, and answers that
don't point, show by the mouse."""
import unittest
from unittest.mock import patch

import cairo

from flippy import overlay, placement, settings, themes

W, H = 1512, 982
NARROW = 248  # the answer card: 1.3 x the question box's 191pt (1.5" on a 14" MacBook)
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

    def test_every_theme_is_the_card_width_with_its_controls_inside_and_apart(self):
        for self.ov.card_w, key in [(w, k) for w in (NARROW, 191) for k in themes.THEMES]:  # and at the box's width
            for text, header, steps in ((SHORT, "Save", ["Save"]), (LONG, None, []), (LONG, "Save", ["a", "b", "c"])):
                with self.subTest(width=self.ov.card_w, theme=key, text=text[:12], steps=len(steps)):
                    settings.set("look", "theme", key)
                    self.ov.clear()
                    self.ov.point(400, 300, "Save")
                    self.ov.show_text(text, header=header, follow=True, controls=True, steps=steps)
                    hits = self.paint()
                    (x, y, w, h), _ = self.ov.card_layout(W, H)
                    self.assertEqual(w, self.ov.card_w)
                    self.assertTrue(hits)
                    for name, (hx, hy, hw, hh) in hits.items():
                        self.assertTrue(x <= hx and hx + hw <= x + w and y <= hy and hy + hh <= y + h, name)
                    names = sorted(hits)
                    for i, a in enumerate(names):
                        for b in names[i + 1:]:
                            # Media Player's big play button sits on its seek bar, as at its own width
                            if not (key == "mediaplayer" and {a, b} == {"seek", "toggle"}):
                                self.assertEqual(placement.overlap(hits[a], hits[b]), 0, f"{a} / {b}")

    def test_every_theme_has_the_slim_row_and_is_shorter_for_it(self):
        for key in [k for k, theme in themes.THEMES.items() if not theme.slim_controls]:
            with self.subTest(theme=key):
                settings.set("look", "theme", key)
                self.ov.clear()
                self.ov.show_text(SHORT, header="Save", controls=True, steps=["Save", "Close"])
                hits = self.paint()
                self.assertTrue({"prev", "toggle", "next", "speed_cycle", "close", "seek"} <= set(hits))
                (_, _, _, slim), opts = self.ov.card_layout(W, H)
                theme = themes.get(key)
                own = theme.card_size(self.ov.card, {**opts, "strip": False})[1]  # its own controls, same width
                self.assertLess(slim, own)

    def test_only_players_when_the_setting_says_so(self):
        settings.set("look", "controls", "players")
        for key in themes.THEMES:
            with self.subTest(theme=key):
                settings.set("look", "theme", key)
                self.ov.clear()
                self.ov.show_text(SHORT, controls=True)
                theme = themes.get(key)
                self.assertEqual(self.ov.card_layout(W, H)[1]["strip"], theme.player and not theme.slim_controls)

    def test_terminal_keeps_its_own_status_line(self):
        settings.set("look", "theme", "terminal")
        self.ov.show_text(SHORT, controls=True)
        self.assertFalse(self.ov.card_layout(W, H)[1]["strip"])
        self.assertIn("toggle", self.paint())

    def test_no_row_while_thinking_or_on_a_task(self):
        for fields in ({"phase": "thinking"}, {"task": True}, {"error": True}):
            with self.subTest(**fields):
                self.ov.clear()
                self.ov.show_text("x", controls=True, **fields)
                self.assertFalse(self.ov.card_layout(W, H)[1]["strip"])

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


class ByTheMouse(Base):
    def rect(self):
        return self.ov.card_layout(W, H)[0]

    def test_thinking_shows_by_the_mouse_and_moves_with_it(self):
        self.ov.at = (700, 500)
        self.ov.show_text("thinking…", phase="thinking")
        x, y, _, _ = self.rect()
        self.assertEqual((x, y), (700 + overlay.MOUSE_GAP, 500 + overlay.MOUSE_GAP + 8))
        self.ov.at = (300, 200)
        x, y, _, _ = self.rect()
        self.assertEqual((x, y), (300 + overlay.MOUSE_GAP, 200 + overlay.MOUSE_GAP + 8))

    def test_an_answer_that_does_not_point_stays_where_it_showed(self):
        self.ov.at = (700, 500)
        self.ov.show_text("thinking…", phase="thinking")
        self.rect()
        self.ov.show_text(SHORT, controls=True)  # [POINT:none]: no pointer to ride with
        before = self.rect()
        self.ov.at = (300, 200)  # reaching for its controls
        self.assertEqual(self.rect(), before)

    def test_left_of_and_above_the_mouse_near_the_edges(self):
        self.ov.at = (W - 20, H - 20)
        self.ov.show_text(LONG, controls=True)
        x, y, w, h = self.rect()
        self.assertLessEqual(x + w, W - 20)
        self.assertLessEqual(y + h, H - 20)

    def test_once_it_points_it_rides_with_the_pointer(self):
        self.ov.at = (700, 500)
        self.ov.show_text("thinking…", phase="thinking")
        self.ov.point(20, 10, "Apple menu")
        self.ov.show_text("The Apple menu.", header="Apple menu", follow=True, controls=True)
        self.assertEqual(self.ov.card_mode, "follow")

    def test_at_the_top_where_the_mouse_is_unknown(self):
        self.ov.show_text("thinking…", phase="thinking")
        self.assertEqual(self.ov.card_mode, "top")
        _, y, _, _ = self.rect()
        self.assertEqual(y, self.ov.card_margin_top)


if __name__ == "__main__":
    unittest.main()
