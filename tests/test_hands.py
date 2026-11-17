"""The pointing hands (flippy/themes.py): index finger by default, the middle finger only as the easter egg."""
import unittest

import cairo

from flippy import settings, themes


def _raised_column(rows):
    """The columns of the finger sticking up above the knuckles (row 0's '#' cells)."""
    return [c for c, ch in enumerate(rows[0]) if ch == "#"]


class Hands(unittest.TestCase):
    def test_the_pixel_hand_points_with_its_index_finger(self):
        # the thumb is on the left, so the index finger is the leftmost of the four
        knuckles = themes.HAND[8]
        first_finger = knuckles.index("#")
        self.assertLessEqual(min(_raised_column(themes.HAND)), first_finger + 1)
        self.assertGreater(min(_raised_column(themes.MIDDLE_HAND)), first_finger + 2)

    def test_tips_sit_in_the_middle_of_the_raised_finger(self):
        for rows, tip in themes.PIXEL_HANDS.values():
            cols = _raised_column(rows)
            self.assertEqual(tip, (cols[0] + cols[-1] + 1) / 2)

    def test_the_glass_hand_curls_its_middle_finger(self):
        finger = themes.GLASS_HAND[0]
        self.assertEqual(finger[:2], (-5, 0))  # the raised finger is first and the tip is at (0, 0)
        others = [p for p in themes.GLASS_HAND[1:] if p[2] <= 10 and p[5] == 0]  # curled fingers
        self.assertTrue(all(p[0] >= finger[0] + finger[2] for p in others))  # all on the side away from the thumb

    def test_every_hand_style_draws_and_has_a_size(self):
        for style in [*themes.PIXEL_HANDS, *themes.GLASS_HANDS]:
            right, left, below, above = themes.pointer_extent(style, 1.0)
            self.assertTrue(right > 0 and left > 0 and below > 0, style)
            surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 200, 200)
            cr = cairo.Context(surf)
            themes.draw_pointer(cr, themes.get("mono"), style, 100, 60, 1.0, 1.0, 10_000)
            surf.flush()
            self.assertTrue(any(surf.get_data()[3::4]), style)

    def test_glass_extents_cover_the_pieces(self):
        for style, shape in themes.GLASS_HANDS.items():
            right, left, _, _ = themes.GLASS_EXTENT[style]
            self.assertGreaterEqual(right, max(p[0] + p[2] for p in shape))
            self.assertGreaterEqual(left, -min(p[0] for p in shape) - 1)

    def test_secret_hands_are_valid_settings(self):
        for style in settings.SECRET_POINTERS:
            self.assertIn(style, [*themes.PIXEL_HANDS, *themes.GLASS_HANDS])
            self.assertEqual(settings.parse_value("look.pointer", style), style)


if __name__ == "__main__":
    unittest.main()
