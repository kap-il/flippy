"""While Flippy thinks, every theme shows it's working (it moves) and offers no playback controls (flippy/themes.py)."""
import unittest

import cairo

from flippy import themes

OPTS = {"text_size": 15, "card_opacity": 0.94, "controls": "all"}
PLAYBACK = {"prev", "next", "toggle", "play", "pause", "stop", "seek", "speed", "speed_cycle"}


def frame(theme, card, t):
    w, h = theme.size(card, OPTS)
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(w) + 4, int(h) + 4)
    theme.draw(cairo.Context(surf), 2, 2, w, h, card, t, OPTS)
    surf.flush()
    return bytes(surf.get_data())


class Thinking(unittest.TestCase):
    def card(self):
        return themes.Card(text="thinking…", phase="thinking", controls=True)

    def test_every_theme_animates_while_thinking(self):
        for key, theme in themes.THEMES.items():
            with self.subTest(theme=key):
                self.assertNotEqual(frame(theme, self.card(), 0.1), frame(theme, self.card(), 0.8))

    def test_no_playback_controls_while_thinking(self):
        for key, theme in themes.THEMES.items():
            with self.subTest(theme=key):
                w, h = theme.size(self.card(), OPTS)
                self.assertFalse(PLAYBACK & set(theme.hit_regions(self.card(), 0, 0, w, h, OPTS)))

    def test_the_dots_cycle(self):
        self.assertEqual([themes.thinking_word(t) for t in (0, 0.34, 0.67, 1.0, 1.34)],
                         ["thinking", "thinking.", "thinking..", "thinking...", "thinking"])


if __name__ == "__main__":
    unittest.main()
