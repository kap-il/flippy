"""The pointer model's color helpers (flippy/pixelart.py): the studio's palette and saved cells go through these."""
import unittest

from flippy import pixelart


class Hex(unittest.TestCase):
    def test_round_trip_and_short_forms(self):
        self.assertEqual(pixelart.to_hex((0.25, 0.6, 1.0)), "#4099FF")
        self.assertEqual(pixelart.from_hex("#4099ff"), (64 / 255, 153 / 255, 1.0))
        self.assertEqual(pixelart.from_hex("fff"), (1.0, 1.0, 1.0))

    def test_junk_is_refused(self):
        for text in ("", "#12345", "zzzzzz", "#1234567"):
            self.assertIsNone(pixelart.from_hex(text))


if __name__ == "__main__":
    unittest.main()
