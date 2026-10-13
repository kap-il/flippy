"""A plain question that asks Flippy to do something becomes a desktop task, without typing /act."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from flippy import brain


def controller(infer=True, platform_acts=True):
    tree = ast.parse((Path(__file__).parents[1] / "flippy/daemon.py").read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Flippy")
    tag = next(n for n in tree.body if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "ACT_TAG")
    names = ("_on_answer", "_stream_text", "_can_infer_act")
    ns = {"event": Mock(), "settings": SimpleNamespace(get=lambda section, key: infer),
          "ACT_TAG": ast.literal_eval(tag.value)}
    for n in cls.body:
        if isinstance(n, ast.FunctionDef) and n.name in names:
            exec(compile(ast.Module(body=[n], type_ignores=[]), "<controller>", "exec"), ns)
    f = type("Controller", (), {name: ns[name] for name in names})()
    f.ui = SimpleNamespace(app_look=Mock()) if platform_acts else SimpleNamespace()
    f.marked = f.input_disabled = False
    f.video = SimpleNamespace(recording=False)
    f.play = SimpleNamespace(gen=1, text="", append=lambda d: setattr(f.play, "text", f.play.text + d),
                             finish=Mock())
    f._owns = lambda req: True
    f._stop_playback = Mock()
    f._fail = Mock()
    f._remember = Mock()  # History (flippy/history_store.py); tested in test_history_store
    f.action_tools = None
    def act(question):
        f.action_tools = object()
        return "desktop task started"
    f.act = Mock(side_effect=act)
    return f


class InferAct(unittest.TestCase):
    def ask(self, f, question, chunks):
        f.act_hold, f.act_question = ("", question) if f._can_infer_act() else (None, None)
        for chunk in chunks:
            f._stream_text(1, chunk)
        f._on_answer(("".join(chunks), 1), None, object())

    def test_act_reply_starts_a_task_with_the_question_and_shows_nothing(self):
        f = controller()
        self.ask(f, "open spotify and play some jazz", ["[A", "CT", "]"])
        f.act.assert_called_once_with("open spotify and play some jazz")
        self.assertEqual(f.play.text, "")
        f._stop_playback.assert_called_once()
        f.play.finish.assert_not_called()

    def test_ordinary_answer_streams_once_it_cant_be_act(self):
        f = controller()
        self.ask(f, "how do i turn on dark mode", ["Open ", "System Settings [POINT:1,2:menu]"])
        f.act.assert_not_called()
        self.assertEqual(f.play.text, "Open System Settings [POINT:1,2:menu]")
        f.play.finish.assert_called_once()

    def test_a_reply_that_only_looks_like_the_start_is_shown_when_it_ends(self):
        f = controller()
        self.ask(f, "what's that", ["["])
        f.act.assert_not_called()
        self.assertEqual(f.play.text, "[")

    def test_task_refused_shows_why(self):
        f = controller()
        f.act = Mock(return_value="Flippy needs the Accessibility permission")
        self.ask(f, "type hello", ["[ACT]"])
        f._fail.assert_called_once_with("Flippy needs the Accessibility permission")

    def test_not_offered_when_off_unsupported_or_about_a_mark(self):
        self.assertFalse(controller(infer=False)._can_infer_act())
        self.assertFalse(controller(platform_acts=False)._can_infer_act())
        f = controller()
        f.marked = True
        self.assertFalse(f._can_infer_act())
        f = controller()
        self.ask(f, "x", ["[ACT]"])  # not offered: the reply is just text
        f.act.assert_called_once()
        f = controller(infer=False)
        self.ask(f, "x", ["[ACT]"])
        f.act.assert_not_called()
        self.assertEqual(f.play.text, "[ACT]")

    def test_the_tutor_prompt_explains_act(self):
        self.assertIn("(you can act", brain.SYSTEM_PROMPT)
        self.assertIn("[ACT]", brain.SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
