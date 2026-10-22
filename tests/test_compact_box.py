"""The compact question box (#37, flippy/mac/ui.py): about 1.5 inches wide, opening next to the mouse and staying on
screen; its short hints; and the system prompt's length rules (#36)."""
import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

from flippy import brain, history

ROOT = Path(__file__).resolve().parents[1]


def ui_functions(*names, **namespace):
    """The named top-level functions and constants of flippy/mac/ui.py, without importing AppKit."""
    tree = ast.parse((ROOT / "flippy/mac/ui.py").read_text())
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names
             or isinstance(n, ast.Assign) and any(getattr(t, "id", None) in names for t in n.targets)]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<ui>", "exec"), namespace)
    return namespace


AREA = (0, 50, 1512, 899)  # a 14" MacBook's visible frame: Dock below, menu bar above (AppKit, origin bottom left)


class Origin(unittest.TestCase):
    def setUp(self):
        ns = ui_functions("box_origin", "MOUSE_GAP")
        self.origin, self.gap = ns["box_origin"], ns["MOUSE_GAP"]

    def test_right_of_and_below_the_mouse(self):
        self.assertEqual(self.origin((600, 500), (190, 70), AREA), (600 + self.gap, 500 - self.gap - 70))

    def test_left_of_the_mouse_near_the_right_edge(self):
        x, _ = self.origin((1490, 500), (190, 70), AREA)
        self.assertEqual(x, 1490 - self.gap - 190)

    def test_above_the_mouse_near_the_bottom(self):
        _, y = self.origin((600, 80), (190, 70), AREA)
        self.assertEqual(y, 80 + self.gap)

    def test_always_inside_the_area(self):
        for mouse in ((0, 0), (1512, 982), (5, 975), (1510, 52), (756, 491)):
            for size in ((190, 70), (190, 300), (280, 899)):
                with self.subTest(mouse=mouse, size=size):
                    x, y = self.origin(mouse, size, AREA)
                    self.assertGreaterEqual(x, AREA[0])
                    self.assertLessEqual(x + size[0], AREA[0] + AREA[2])
                    self.assertGreaterEqual(y, AREA[1])
                    self.assertLessEqual(y + size[1], AREA[1] + AREA[3])


class Width(unittest.TestCase):
    def width(self, points_wide, mm_wide):
        quartz = SimpleNamespace(CGDisplayScreenSize=lambda display: SimpleNamespace(width=mm_wide))
        ns = ui_functions("box_width", "BOX_INCHES", "BOX_PT", "BOX_FALLBACK_PT", Quartz=quartz)
        screen = SimpleNamespace(deviceDescription=lambda: {"NSScreenNumber": 1},
                                 frame=lambda: SimpleNamespace(size=SimpleNamespace(width=points_wide)))
        return ns["box_width"](screen), ns

    def test_one_and_a_half_inches_on_a_macbook(self):
        w, _ = self.width(1512, 302)  # 14" MacBook Pro at its default scaling
        self.assertAlmostEqual(w, 1.5 * 25.4 * 1512 / 302, delta=1)

    def test_kept_within_bounds_on_a_big_display(self):
        w, ns = self.width(2560, 300)  # a dense display: 1.5" would be far more points than the box needs
        self.assertEqual(w, ns["BOX_PT"][1])

    def test_a_display_that_does_not_report_its_size(self):
        w, ns = self.width(1512, 0)
        self.assertEqual(w, ns["BOX_FALLBACK_PT"])


class Hints(unittest.TestCase):
    def test_every_hint_has_a_short_form(self):
        for hint in (history.BOX_HINT, history.FOLLOW_UP_HINT, history.SECOND_FOLLOW_UP_HINT):
            with self.subTest(hint=hint):
                self.assertLess(len(history.SHORT_HINTS[hint]), len(hint))

    def test_follow_up_hints_still_say_what_esc_does(self):
        self.assertIn("esc back", history.SHORT_HINTS[history.FOLLOW_UP_HINT])
        self.assertIn("esc close", history.SHORT_HINTS[history.SECOND_FOLLOW_UP_HINT])


class AnswerLength(unittest.TestCase):
    def test_the_prompt_sets_a_concrete_length(self):
        self.assertIn("under 30 words", brain.SYSTEM_PROMPT)
        self.assertIn("one short line per thing (under 12 words)", brain.SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
