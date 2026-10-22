"""A thinking card's "…" cycles through ., .., ... in every theme (flippy/overlay.py OverlayBase._shown)."""
import unittest
from unittest.mock import patch

import cairo

from flippy import overlay, settings, themes

W, H = 1512, 982


class Overlay(overlay.OverlayBase):
    def queue_draw(self): pass
    def set_opacity(self, a): pass
    def start_ticking(self): pass
    def stop_ticking(self): pass
    def set_drawing_input(self, on): pass


class Dots(unittest.TestCase):
    def test_the_dots_cycle(self):
        card = themes.Card(text="thinking…", phase="thinking")
        shown = [overlay.OverlayBase._shown(card, i / overlay.DOTS_PER_S).text for i in range(5)]
        self.assertEqual(shown, ["thinking", "thinking.", "thinking..", "thinking...", "thinking"])

    def test_only_thinking_cards_ending_in_an_ellipsis(self):
        for card in (themes.Card(text="Saved…"), themes.Card(text="thinking", phase="thinking")):
            with self.subTest(text=card.text, phase=card.phase):
                self.assertIs(overlay.OverlayBase._shown(card, 1.0), card)

    def test_every_theme_draws_them_and_keeps_its_size(self):
        with patch.object(settings, "_save"):
            old = settings.get("look", "theme")
            self.addCleanup(settings.set, "look", "theme", old)
            for key in themes.THEMES:
                with self.subTest(theme=key):
                    settings.set("look", "theme", key)
                    ov = Overlay()
                    ov.show_text("thinking…", phase="thinking")
                    drawn, sizes = [], set()
                    real = ov.theme.draw
                    with patch.object(type(ov.theme), "draw", lambda self, cr, x, y, w, h, card, t, opts:
                                      (drawn.append(card.text), real(cr, x, y, w, h, card, t, opts))):
                        for step in range(4):
                            ov.card_t0 = overlay.time.monotonic() - step / overlay.DOTS_PER_S - 0.01
                            ov.paint(cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)), W, H)
                            sizes.add(tuple(ov.card_layout(W, H)[0][2:]))
                    self.assertEqual(drawn, ["thinking", "thinking.", "thinking..", "thinking..."])
                    self.assertEqual(len(sizes), 1)
                    self.assertEqual(ov.card.text, "thinking…")  # the card itself is untouched


if __name__ == "__main__":
    unittest.main()
