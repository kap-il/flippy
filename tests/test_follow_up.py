"""A follow-up question in the middle of a walkthrough, answered on the side, then back to the step (#26)."""
import types
import unittest
from unittest.mock import MagicMock, patch

from flippy import daemon, settings
from flippy.playback import Options, Playback
from flippy.requests import Request

WALKTHROUGH = "Open File [POINT:10,20:File] then Export [POINT:30,40:Export:click] done."


def at(point):
    return (point.x, point.y)


class Parking(unittest.TestCase):
    def test_parked_step_stops_waiting_and_is_pointed_again(self):
        p = Playback(0)
        p.finish(WALKTHROUGH)
        o = Options(tutorial=True, clicks_available=True)
        p.step, p.shown = 1, 99
        events = p.tick(1, o, at)
        self.assertIn(("watch_clicks", True), events)
        self.assertEqual(p.park(), [("watch_clicks", False)])
        self.assertTrue(p.paused)
        self.assertFalse(p.waiting)
        p.resume(5)
        self.assertFalse(p.paused)
        events = p.tick(5.1, o, at)
        self.assertEqual(events[0][0], "point")  # the pointer goes back to the step
        self.assertIn(("watch_clicks", True), events)  # and it waits for its click again
        self.assertEqual(p.step, 1)

    def test_parked_while_waiting_for_more_it_holds_the_step_showing(self):
        p = Playback(0)
        p.append("Open File [POINT:10,20:File] then")
        o = Options()
        p.tick(0.1, o, at)
        p.tick(1, o, at)
        p.tick(9, o, at)  # its hold is over: on to a step not in yet
        self.assertEqual(p.step, 1)
        p.park()
        self.assertEqual(p.step, 0)
        self.assertEqual(p.card(10, o)["step"], 0)

    def test_resume_after_the_end_plays_the_step_again(self):
        p = Playback(0)
        p.finish("Only this [POINT:1,2:x]")
        o = Options()
        p.tick(1, o, at)
        p.tick(9, o, at)
        self.assertTrue(p.finished)
        p.park()
        p.resume(10)
        self.assertFalse(p.finished)
        self.assertEqual(p.tick(10.1, o, at)[0][0], "point")


def flippy():
    """The real controller methods, on a stand-in with no native UI."""
    f = daemon.Flippy.__new__(daemon.Flippy)
    f.gen, f.busy, f.quitting, f.tutorial = 5, False, False, True
    f.request = None
    f.action_tools = None
    f.marked = f.input_disabled = False
    f.play_id = f.fade_id = f.fade_animation_id = 0
    f.followup_token = f.focus_at_open = None
    f.act_hold = None
    f.overlay = MagicMock(drawing=False, card=None)
    f.box = types.SimpleNamespace(visible=False, show=MagicMock(), hide=MagicMock())
    f.ui = types.SimpleNamespace(screen_size=lambda: (1000, 800), scale=lambda: 1.0)
    f.video = types.SimpleNamespace(recording=False)
    pl = Playback(0)
    pl.gen, pl.img, pl.shot, pl.frame, pl.asked = 5, (1000, 800), (1000, 800), None, True
    pl.append(WALKTHROUGH)
    f.play = pl
    return f


class Queue:
    def __init__(self):
        self.calls = []

    def timeout_add(self, ms, fn):
        self.calls.append(fn)
        return len(self.calls)

    idle_add = lambda self, fn: self.timeout_add(0, fn)

    def source_remove(self, timer):
        pass

    def run(self):
        calls, self.calls = self.calls, []
        for fn in calls:
            if fn():
                self.calls.append(fn)


class Keycaps(unittest.TestCase):
    def test_a_hotkey_splits_into_keys(self):
        from flippy.themes import split_keys
        self.assertEqual(split_keys("⇧⌘Space"), ["⇧", "⌘", "Space"])
        self.assertEqual(split_keys("⌃⌥A"), ["⌃", "⌥", "A"])
        self.assertEqual(split_keys("Super+Shift+Space"), ["Super", "Shift", "Space"])


class FollowUp(unittest.TestCase):
    def setUp(self):
        self.queue = Queue()
        for p in (patch.object(daemon, "loop", self.queue), patch.object(daemon, "event"),
                  patch.object(settings, "_save")):
            p.start()
            self.addCleanup(p.stop)

    def follow_up(self, f):
        f.play.tick(0.1, f._play_options(), f._play_coords)
        f.play.shown = 99
        daemon.Flippy.open_box(f)  # the ask hotkey, with a walkthrough on screen
        return f.parked

    def test_the_hotkey_sets_the_walkthrough_aside_and_esc_goes_back(self):
        f = flippy()
        pl = self.follow_up(f)
        self.assertIsNotNone(pl)
        self.assertIsNone(f.play)
        self.assertTrue(pl.paused)
        f.box.show.assert_called_once()
        self.assertTrue(f.overlay.show_text.call_args.kwargs["paused"])  # its card stays up, paused
        f.box.visible = True
        f._box_cancel()  # Esc in the question box
        self.assertIs(f.play, pl)
        self.assertIsNone(f.parked)
        self.assertFalse(pl.paused)
        self.assertTrue(f.tutorial)
        f.box.hide.assert_called()

    def test_the_question_carries_the_step_and_is_answered_on_the_side(self):
        f = flippy()
        self.follow_up(f)
        f._run_request, f._ask = MagicMock(), MagicMock()
        f._begin_request = lambda mode: Request(6, mode)
        f.submit("where is that menu?")
        question = f._ask.call_args.args[0]
        self.assertTrue(question.startswith('[A follow-up in the middle of your walkthrough, at step 1 of 2 so far: '
                                            '"Open File" (you\'re pointing at File).'))
        self.assertIn("where is that menu?", question)
        self.assertIn("not a tutorial", question)
        self.assertFalse(f.tutorial)
        self.assertIsNotNone(f.parked)  # kept until they go back or drop it
        self.assertEqual(f._resume_action(), {"actions": (("resume", "← Previous"),)})

    def test_a_walkthrough_still_streaming_finishes_before_the_follow_up_is_asked(self):
        f = flippy()
        req = f.request = Request(5, "tutor")
        f.busy = True
        pl = self.follow_up(f)
        req.cancel = MagicMock()
        f.submit("why?")
        req.cancel.assert_not_called()  # the rest of the walkthrough keeps coming
        f._stream_text(5, " More.")
        self.assertTrue(pl.raw.endswith("More."))
        f._ask, f._run_request = MagicMock(), MagicMock()
        f._begin_request = lambda mode: Request(6, mode)
        self.queue.run()
        f._ask.assert_not_called()  # still waiting
        req.drained.set()
        f.busy = False
        self.queue.run()
        self.assertIn("why?", f._ask.call_args.args[0])

    def test_a_follow_up_that_changes_the_plan_replaces_the_rest(self):
        f = flippy()
        self.follow_up(f)
        f._run_request, f._ask = MagicMock(), MagicMock()
        f._begin_request = lambda mode: Request(6, mode)
        f.submit("actually I want to print it")
        self.assertIn(daemon.PLAN_TAG, f._ask.call_args.args[0])  # Claude is told how
        self.assertFalse(f.tutorial)
        f.gen = 6
        f._start_playback(6, (1000, 800), (1000, 800), asked=True)
        for delta in ("[NEW", " PLAN] Open ", "Print [POINT:5,6:Print]."):
            f._stream_text(6, delta)
        self.assertIsNone(f.parked)  # the walkthrough set aside is gone...
        self.assertTrue(f.tutorial)  # ...and the new steps wait for clicks like it did
        self.assertEqual(f.play.raw, "Open Print [POINT:5,6:Print].")
        self.assertEqual(f._resume_action(), {})

    def test_a_new_plan_held_to_the_end_still_replaces_the_rest(self):
        f = flippy()
        self.follow_up(f)
        f._run_request, f._ask = MagicMock(), MagicMock()
        f._begin_request = lambda mode: Request(6, mode)
        f._owns = lambda req: True
        f.submit("print instead")
        f.gen = 6
        f._start_playback(6, (1000, 800), (1000, 800), asked=True)
        f._stream_text(6, "[NEW PLAN]")
        f._on_answer(("[NEW PLAN]Print [POINT:5,6:Print].", 6), None, Request(6, "tutor"))
        self.assertIsNone(f.parked)
        self.assertEqual(f.play.raw, "Print [POINT:5,6:Print].")

    def test_a_plain_side_answer_keeps_the_walkthrough(self):
        f = flippy()
        self.follow_up(f)
        f._run_request, f._ask = MagicMock(), MagicMock()
        f._begin_request = lambda mode: Request(6, mode)
        f.submit("what's that?")
        f.gen = 6
        f._start_playback(6, (1000, 800), (1000, 800), asked=True)
        f._stream_text(6, "[")
        self.assertEqual(f.play.raw, "")  # could still be the tag
        f._stream_text(6, "File] is the menu.")
        self.assertEqual(f.play.raw, "[File] is the menu.")
        self.assertIsNotNone(f.parked)

    def test_the_box_says_esc_goes_back(self):
        f = flippy()
        self.follow_up(f)
        self.assertEqual(f.box.show.call_args.kwargs, {"hint": daemon.FOLLOW_UP_HINT})

    def test_a_side_answer_ticks_no_tour_step(self):
        f = flippy()
        self.follow_up(f)
        f._play_events([("finish", 5)])
        names = [c.args[0] for c in daemon.event.call_args_list]
        self.assertIn("side_answer_done", names)
        self.assertNotIn("answer_done", names)

    def test_the_hotkey_while_drawing_a_mark_for_a_follow_up_keeps_the_walkthrough(self):
        f = flippy()
        pl = self.follow_up(f)
        f.overlay.drawing = True
        def cancel_draw():  # (the real one goes back to a walkthrough set aside)
            f.overlay.drawing = False
            if f.parked:
                f._resume_walkthrough()
        f.cancel_draw = cancel_draw
        f.box.show.reset_mock()
        daemon.Flippy.open_box(f)
        self.assertIs(f.parked, pl)
        f.box.show.assert_called_once()

    def test_a_queued_follow_up_says_so_and_waits_for_a_second_one(self):
        f = flippy()
        req = f.request = Request(5, "tutor")
        f.busy = True
        self.follow_up(f)
        f.submit("why?")
        self.assertEqual(f.overlay.show_text.call_args.kwargs["actions"][0], (None, "Asking when the rest is in…"))
        f.box.visible = True  # typing another
        f._ask, f._run_request = MagicMock(), MagicMock()
        f._begin_request = lambda mode: Request(6, mode)
        req.drained.set()
        f.busy = False
        self.queue.run()
        f._ask.assert_not_called()
        f.busy = True
        f.submit("and where?")
        f.box.visible = f.busy = False
        self.queue.run()
        f._ask.assert_called_once()
        self.assertIn("why?\nand where?", f._ask.call_args.args[0])

    def test_esc_on_a_second_follow_up_keeps_the_first_waiting(self):
        f = flippy()
        f.request = Request(5, "tutor")
        f.busy = True
        pl = self.follow_up(f)
        f.submit("why?")
        f.box.visible = True
        f._box_cancel()  # Esc: never mind the second one
        self.assertIs(f.parked, pl)
        self.assertEqual(f.followup_pending, "why?")
        f.box.hide.assert_called()
        daemon.Flippy.open_box(f)  # the hotkey again does the same
        self.assertIs(f.parked, pl)
        f.control("resume")  # the way back drops it
        self.assertIsNone(f.followup_pending)
        self.assertFalse(f.may_replan)

    def test_esc_on_a_second_follow_up_keeps_the_side_answer_showing(self):
        f = flippy()
        pl = self.follow_up(f)
        f.gen = 6
        f._start_playback(6, (1000, 800), (1000, 800), asked=True)  # the answer to "why?", on the side
        side = f.play
        daemon.Flippy.open_box(f)
        f.box.visible = True
        f._box_cancel()
        self.assertIs(f.play, side)
        self.assertIs(f.parked, pl)  # "← Previous" still goes back
        self.assertEqual(f.box.show.call_args.kwargs, {"hint": daemon.SECOND_FOLLOW_UP_HINT})  # Esc closes
        f._schedule_fade = MagicMock()
        side.finished = side.paused = True  # done, and paused to read it: it stays
        f._box_cancel()
        f._schedule_fade.assert_not_called()
        side.paused = False  # a side answer done, its fade stopped by the hotkey
        f._box_cancel()
        f._schedule_fade.assert_called_once_with(settings.get("timing", "max_show_seconds"))

    def test_a_queued_follow_up_lost_with_its_walkthrough_says_so(self):
        f = flippy()
        req = f.request = Request(5, "tutor")
        f.busy = True
        self.follow_up(f)
        f.submit("why?")
        f._owns = lambda r: True
        f._fail = MagicMock()
        with patch.object(daemon.errors, "describe", return_value=("It failed.", "X")), \
                patch.object(daemon, "log"), patch.object(daemon.Flippy, "_open_setup_if"):
            f._on_answer(None, RuntimeError("x"), req)
        self.assertIsNone(f.parked)
        self.assertEqual(f._fail.call_args.kwargs, {"actions": ((None, daemon.LOST_FOLLOW_UP),)})
        self.assertIsNone(f.followup_pending)

    def test_back_to_the_step_drops_a_side_answer_still_coming(self):
        f = flippy()
        pl = self.follow_up(f)
        side = f.request = Request(6, "tutor")
        f.busy = True
        f.control("resume")
        self.assertTrue(side.canceled.is_set())
        self.assertIs(f.play, pl)

    def test_dismiss_or_a_command_drops_it(self):
        f = flippy()
        self.follow_up(f)
        with patch.object(daemon.Flippy, "open_settings"):
            f.submit("/settings")
        self.assertIsNotNone(f.parked)  # (settings: the walkthrough's still there after)
        with patch.object(daemon.Flippy, "reset_session"):
            f.submit("/new")
        self.assertIsNone(f.parked)
        f = flippy()
        self.follow_up(f)
        with patch.object(daemon.Flippy, "cancel_draw"):
            f.dismiss()
        self.assertIsNone(f.parked)

    def test_a_mark_drawn_for_the_follow_up_goes_with_esc(self):
        f = flippy()
        self.follow_up(f)
        f.marked = True
        f.box.visible = True
        f._box_cancel()
        self.assertFalse(f.marked)
        self.assertEqual(f.overlay.strokes, [])

    def test_its_pin_and_place_come_back_after_a_side_answer(self):
        f = flippy()
        f.overlay.pinned, f.overlay.avoid = True, (1, 2, 3, 4)
        self.follow_up(f)
        f.overlay.pinned, f.overlay.avoid = False, None  # the side answer's
        f.control("resume")
        self.assertTrue(f.overlay.pinned)
        self.assertEqual(f.overlay.avoid, (1, 2, 3, 4))

    def test_cancelling_a_drawing_goes_back_to_the_walkthrough(self):
        f = flippy()
        pl = self.follow_up(f)
        f._end_draw_mode = MagicMock()
        f.cancel_draw()
        self.assertIs(f.play, pl)
        self.assertIsNone(f.parked)

    def test_its_own_controls_go_back_to_it(self):
        f = flippy()
        pl = self.follow_up(f)
        f.control("next")
        self.assertIs(f.play, pl)
        self.assertEqual(pl.step, 1)

    def test_a_one_step_answer_is_not_a_walkthrough(self):
        f = flippy()
        f.play = Playback(0)
        f.play.gen, f.play.frame, f.play.img, f.play.shot, f.play.asked = 5, None, (1000, 800), (1000, 800), True
        f.play.finish("It's in the File menu [POINT:10,20:File]")
        f.play.tick(0.1, f._play_options(), f._play_coords)
        f._render_step(f.play, 0.2)
        self.assertNotIn("actions", f.overlay.show_text.call_args.kwargs)
        daemon.Flippy.open_box(f)
        self.assertIsNone(f.parked)  # the hotkey asks as before
        f = flippy()
        f.play.asked = False  # a video review's or a demo's
        self.assertFalse(f._walkthrough_up())

    def test_a_walkthrough_card_says_how_to_follow_up(self):
        f = flippy()
        f.play.tick(0.1, f._play_options(), f._play_coords)
        f._render_step(f.play, 0.2)
        self.assertNotIn("actions", f.overlay.show_text.call_args.kwargs)  # no ask hotkey: nothing to say
        f = flippy()
        f.ui.key_label = lambda name: "⇧⌘Space"
        f.play.tick(0.1, f._play_options(), f._play_coords)
        f._render_step(f.play, 0.2)
        hint = (None, "Follow up", ("⇧", "⌘", "Space"))
        self.assertEqual(f.overlay.show_text.call_args.kwargs["actions"], (hint,))
        f.ui.key_label = lambda name: "⌃⌥A"  # the hotkey changed in Settings
        get = settings.get
        with patch.object(settings, "get", lambda section, key: "ctrl+alt+a" if key == "ask" else get(section, key)):
            f._render_step(f.play, 0.3)
        self.assertEqual(f.overlay.show_text.call_args.kwargs["actions"], ((None, "Follow up", ("⌃", "⌥", "A")),))


class AskThenActAndBack(unittest.TestCase):
    """What was just asked or done goes along with what's next (#26)."""
    def setUp(self):
        self.queue = Queue()
        for p in (patch.object(daemon, "loop", self.queue), patch.object(daemon, "event"),
                  patch.object(settings, "_save")):
            p.start()
            self.addCleanup(p.stop)

    def test_a_task_after_an_answer_knows_the_question_and_answer(self):
        f = flippy()
        f._remember("ask", "how do I export a PDF?", "Click File [POINT:1,2:File] then Export.")
        context = f._task_context()
        self.assertIn('I asked Flippy: "how do I export a PDF?"', context)
        self.assertTrue(context.endswith("The task: "))

    def test_finished_answer_saves_history_and_keeps_follow_up_context(self):
        f = flippy()
        f._run_request, f._ask = MagicMock(), MagicMock()
        req = Request(7, "tutor")
        f._begin_request = lambda mode: req
        f._owns = lambda owner: owner is req
        with patch.object(daemon.history_store, "add") as save:
            f.submit("how do I export a PDF?")
            f._on_answer(("Open File [POINT:1,2:File].", 7), None, req)
        self.assertEqual(save.call_args.args[:3], ("ask", "how do I export a PDF?", "Open File."))
        self.assertIsNone(req.history)
        self.assertEqual(f.last_exchange["question"], "how do I export a PDF?")
        self.assertEqual(f.last_exchange["answer"], "Open File.")
        self.assertIn("how do I export a PDF?", f._task_context())

    def test_a_task_after_a_task_knows_how_it_ended(self):
        f = flippy()
        f._remember("task", "play some jazz", "Playing Kind of Blue.")
        self.assertIn('you did a task for me: "play some jazz". It ended: "Playing Kind of Blue."', f._task_context())

    def test_a_question_after_a_task_knows_it_once(self):
        f = flippy()
        f._remember("task", "play some jazz", "Playing Kind of Blue.")
        self.assertIn('desktop task for me: "play some jazz"', f._ask_context())
        self.assertEqual(f._ask_context(), "")  # the conversation has it from here
        self.assertIn('"play some jazz"', f._task_context())  # ...but "now turn it up" can still become a task

    def test_a_walkthrough_continued_after_your_click_is_still_your_question(self):
        f = flippy()
        f._run_request, f._ask = MagicMock(), MagicMock()
        f._begin_request = lambda mode: Request(7, mode)
        f._owns = lambda req: True
        f.submit("how do I export a PDF?")
        f._on_answer(("Open File [POINT:1,2:File].", 7), None, Request(7, "tutor"))
        f.submit(daemon.CONTINUE, tutorial=True)
        f._on_answer(("Now click Export [POINT:3,4:Export].", 7), None, Request(7, "tutor"))
        self.assertEqual(f.last_exchange["question"], "how do I export a PDF?")
        self.assertEqual(f.last_exchange["answer"], "Open File. Now click Export.")
        self.assertEqual(daemon._cut("one two three", 9, tail=True), "…wo three")  # a long one keeps its latest steps

    def test_back_from_a_side_answer_the_walkthrough_is_the_latest_again(self):
        f = flippy()
        f._run_request, f._ask = MagicMock(), MagicMock()
        f._owns = lambda req: True
        f.asked = "how do I export a PDF?"
        f._on_answer((WALKTHROUGH, 5), None, Request(5, "tutor"))
        f.play.tick(0.1, f._play_options(), f._play_coords)
        f.play.shown = 99
        daemon.Flippy.open_box(f)
        f._begin_request = lambda mode: Request(6, mode)
        f.submit("what's PDF/A?")
        f._on_answer(("PDF/A is for archives.", 6), None, Request(6, "tutor"))
        self.assertEqual(f.last_exchange["question"], "what's PDF/A?")  # "do it for me" now: the side question
        f.control("resume")
        self.assertEqual(f.last_exchange["question"], "how do I export a PDF?")
        self.assertEqual(f.asked, "how do I export a PDF?")

    def test_nothing_after_a_long_break(self):
        f = flippy()
        f._remember("task", "play some jazz", "Playing Kind of Blue.")
        f.last_exchange["at"] -= daemon.IDLE_RESET_S + 1
        self.assertEqual(f._ask_context(), "")
        self.assertEqual(f._task_context(), "")

    def test_act_in_a_follow_up_carries_the_step(self):
        f = flippy()
        f.play.tick(0.1, f._play_options(), f._play_coords)
        f.play.shown = 99
        daemon.Flippy.open_box(f)
        f.act = MagicMock(return_value="started")
        f.action_tools = object()
        f.submit("/act do it for me")
        f.act.assert_called_once()
        self.assertEqual(f.act.call_args.args[0], "do it for me")
        self.assertIn('step 1 of 2 so far of your walkthrough: "Open File" (pointing at File)',
                      f.act.call_args.kwargs["step"])
        self.assertIsNone(f.parked)

    def test_a_follow_up_can_become_a_task(self):
        f = flippy()
        f.play.tick(0.1, f._play_options(), f._play_coords)
        f.play.shown = 99
        daemon.Flippy.open_box(f)
        f._run_request, f._ask = MagicMock(), MagicMock()
        f._begin_request = lambda mode: Request(6, mode)
        f.ui.app_look = MagicMock()
        get = settings.get
        with patch.object(settings, "get", lambda section, key: True if key == "infer" else get(section, key)):
            f.submit("just do this step for me")
        self.assertEqual(f.act_hold, "")  # an [ACT] reply would start the task...
        self.assertIn("step 1 of 2", f.act_step)  # ...with the step it's about


if __name__ == "__main__":
    unittest.main()
