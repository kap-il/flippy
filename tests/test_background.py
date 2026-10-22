"""Background desktop tasks (flippy/actions.py BACKGROUND_CATALOG): controls by number, refusals that let the
task go on, and what approval cards say."""
import asyncio
import sys
import threading
import unittest

from flippy.actions import (BACKGROUND_CATALOG, BACKGROUND_PROMPT, AppFrame, DesktopTools, RetryableActionError)

TARGET = ("com.apple.textedit", 4242, 7, (0, 0, 100, 100), 0)


def frame(n=2):
    elements = {1: (object(), "AXTextArea", "body", [], True), 2: (object(), "AXButton", "Save", ["AXPress"], False)}
    return AppFrame("anBn", (100, 100), TARGET, dict(list(elements.items())[:n]), "App: TextEdit\n[1] text area\n[2] button")


class Background(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.done = []
        self.approved = []
        self.fail_with = None

        async def capture():
            return frame()

        async def approve(name, args, shot):
            self.approved.append(name)
            return True

        async def perform(name, args, shot, cancel):
            if self.fail_with:
                raise self.fail_with
            self.done.append((name, args))
        self.tools = DesktopTools(capture, approve, perform, approval_mode="per_app", max_actions=40, max_text=2000,
                                  catalog=BACKGROUND_CATALOG, prompt=BACKGROUND_PROMPT)

    async def test_look_then_act_on_controls_by_number(self):
        r = await self.tools.invoke("look", {})
        self.assertIn("App: TextEdit", r["content"][0]["text"])
        r = await self.tools.invoke("press", {"element": 2, "reason": "save"})
        self.assertNotIn("is_error", r)
        r = await self.tools.invoke("set_text", {"element": 1, "text": "hello", "reason": "write"})
        self.assertNotIn("is_error", r)
        await self.tools.invoke("menu", {"path": ["File", "New"], "reason": "new doc"})
        self.assertEqual([n for n, _ in self.done], ["press", "set_text", "menu"])
        self.assertEqual(self.tools.actions, 3)

    async def test_press_many_presses_each_from_the_same_look_in_one_step(self):
        await self.tools.invoke("look", {})
        shots = []
        perform = self.tools.perform
        async def record(name, args, shot, cancel):
            shots.append(shot)
            await perform(name, args, shot, cancel)
        self.tools.perform = record
        r = await self.tools.invoke("press_many", {"elements": [2, 1, 2], "reason": "keypad"})
        self.assertNotIn("is_error", r)
        self.assertEqual([(n, a["element"]) for n, a in self.done], [("press", 2), ("press", 1), ("press", 2)])
        self.assertEqual(len({id(s) for s in shots}), 1)  # numbers from one look, even if the app changes between
        self.assertEqual((self.approved, self.tools.actions, self.tools.timing["steps"]), (["press_many"], 3, 2))

    async def test_press_many_checks_every_number_first(self):
        await self.tools.invoke("look", {})
        r = await self.tools.invoke("press_many", {"elements": [2, 99], "reason": "keypad"})
        self.assertTrue(r["is_error"])
        self.assertEqual(self.done, [])  # nothing pressed when one number is wrong
        self.tools.max_actions = 2
        r = await self.tools.invoke("press_many", {"elements": [2, 2, 2], "reason": "keypad"})
        self.assertTrue(r["is_error"])
        self.assertEqual(self.done, [])

    async def test_use_app_first_needs_no_look(self):
        r = await self.tools.invoke("use_app", {"name": "TextEdit", "reason": "open it"})
        self.assertNotIn("is_error", r)
        self.assertEqual([n for n, _ in self.done], ["use_app"])

    async def test_screenshots_only_when_needed(self):
        from flippy import actions
        many = {i: (object(), "AXButton", f"b{i}", ["AXPress"], False) for i in range(1, actions.TEXT_ONLY_CONTROLS + 1)}
        rich = AppFrame("anBn", (100, 100), TARGET, many, "App: Calculator")
        sparse = frame()
        self.tools.capture = lambda: asyncio.sleep(0, rich)
        images = lambda r: [c for c in r["content"] if c["type"] == "image"]
        self.assertTrue(images(await self.tools.invoke("look", {})))  # asked to look: it sees
        r = await self.tools.invoke("press", {"element": 2, "reason": "x"})
        self.assertFalse(images(r))  # the list covers it
        self.assertIn("Call look", r["content"][-1]["text"])
        self.assertTrue(images(await self.tools.invoke("click", {"x": 5, "y": 5, "count": 1, "real_pointer": False,
                                                                  "reason": "x"})))  # by position: checked by eye
        self.tools.capture = lambda: asyncio.sleep(0, sparse)
        await self.tools.invoke("look", {})
        self.assertTrue(images(await self.tools.invoke("press", {"element": 2, "reason": "x"})))  # says little

    async def test_unknown_control_number_is_a_retry_not_a_stop(self):
        await self.tools.invoke("look", {})
        r = await self.tools.invoke("press", {"element": 99, "reason": "x"})
        self.assertTrue(r["is_error"])
        self.assertIn("Not done", r["content"][0]["text"])
        self.assertIn("App: TextEdit", r["content"][1]["text"])  # a fresh look comes with it
        self.assertFalse(self.tools.cancel.is_set())
        self.assertEqual(self.done, [])

    async def test_app_refusal_is_a_retry_not_a_stop(self):
        await self.tools.invoke("look", {})
        self.fail_with = RetryableActionError("Format > Bold is greyed out right now.")
        r = await self.tools.invoke("menu", {"path": ["Format", "Bold"], "reason": "bold"})
        self.assertIn("greyed out", r["content"][0]["text"])
        self.assertFalse(self.tools.cancel.is_set())
        self.fail_with = None
        r = await self.tools.invoke("press", {"element": 2, "reason": "save"})
        self.assertNotIn("is_error", r)

    async def test_click_scroll_drag_by_position_in_the_screenshot(self):
        self.assertIn("click", self.tools.catalog())
        await self.tools.invoke("look", {})
        await self.tools.invoke("click", {"x": 50, "y": 40, "count": 1, "real_pointer": False, "reason": "play"})
        await self.tools.invoke("scroll", {"x": 50, "y": 40, "direction": "down", "lines": 3, "real_pointer": False,
                                           "reason": "more"})
        await self.tools.invoke("drag", {"x": 10, "y": 10, "to_x": 60, "to_y": 60, "real_pointer": False,
                                         "reason": "move"})
        await self.tools.invoke("click", {"x": 50, "y": 40, "count": 2, "real_pointer": True, "reason": "again"})
        self.assertEqual([n for n, _ in self.done], ["click", "scroll", "drag", "click"])
        self.assertTrue(self.done[-1][1]["real_pointer"])
        self.assertFalse(self.tools.cancel.is_set())

    async def test_a_spot_off_the_screenshot_is_a_retry_not_a_stop(self):
        await self.tools.invoke("look", {})
        r = await self.tools.invoke("click", {"x": 500, "y": 40, "count": 1, "real_pointer": False, "reason": "x"})
        self.assertTrue(r["is_error"])
        self.assertIn("outside the screenshot", r["content"][0]["text"])
        self.assertFalse(self.tools.cancel.is_set())
        self.assertEqual(self.done, [])


class NoWindow(unittest.IsolatedAsyncioTestCase):
    async def test_a_look_without_a_window_is_text_only_and_media_works(self):
        done = []

        async def capture():
            return AppFrame(None, (0, 0), TARGET, {}, "App: Spotify — no window open.\nMenus: Playback: Next")

        async def approve(name, args, shot):
            return True

        async def perform(name, args, shot, cancel):
            done.append((name, args))
        tools = DesktopTools(capture, approve, perform, approval_mode="per_app", max_actions=40, max_text=2000,
                             catalog=BACKGROUND_CATALOG, prompt=BACKGROUND_PROMPT)
        r = await tools.invoke("look", {})
        self.assertEqual([c["type"] for c in r["content"]], ["text"])  # no image, still a look
        await tools.invoke("media", {"action": "next", "reason": "skip"})
        await tools.invoke("menu", {"path": ["Playback", "Next"], "reason": "skip"})
        self.assertEqual([n for n, _ in done], ["media", "menu"])
        r = await tools.invoke("media", {"action": "eject", "reason": "x"})
        self.assertTrue(r["is_error"])


@unittest.skipUnless(sys.platform == "darwin", "macOS key codes")
class Keys(unittest.TestCase):
    def test_every_key_offered_to_claude_has_a_key_code(self):
        from flippy.actions import APP_KEYS
        from flippy.mac import ax
        for combo in APP_KEYS:
            name = combo.split("+")[-1]
            self.assertIsNotNone(ax.NAV_KEYS.get(name, ax.hotkeys.KEYS.get(name)), combo)


class Coordinates(unittest.TestCase):
    def test_screenshot_pixels_map_to_the_window_on_screen(self):
        f = AppFrame("x", (200, 100), ("com.apple.textedit", 1, 7, (300, 50, 100, 50), 0), {}, "")
        self.assertEqual(f.to_logical(0, 0), (300, 50))
        self.assertEqual(f.to_logical(100, 50), (350, 75))  # a 2x screenshot: half the points

    def test_no_screenshot_means_no_clicking_by_position(self):
        f = AppFrame(None, (0, 0), TARGET, {}, "")
        with self.assertRaises(RetryableActionError):
            f.to_logical(1, 1)


class Describe(unittest.TestCase):
    def test_cards_say_what_will_happen_in_words(self):
        f = frame()
        self.assertTrue(f.describe("press", {"element": 2, "reason": "r"}).startswith("press 'Save'"))
        self.assertTrue(f.describe("menu", {"path": ["File", "New"], "reason": "r"}).startswith("choose File > New"))
        self.assertIn('"hi"', f.describe("type", {"text": "hi", "reason": "r"}))
        self.assertEqual(f.describe("use_app", {"name": "Spotify", "reason": "Play a song"}), "switch to Spotify\nPlay a song")
        self.assertIn('"\\u202e', f.describe("type", {"text": "\u202e", "reason": "r"}))


if __name__ == "__main__":
    unittest.main()


class PressManyCard(unittest.TestCase):
    def test_the_card_names_each_control(self):
        self.assertIn("press 'Save', 'body'", frame().describe("press_many", {"elements": [2, 1], "reason": "r"}))


class StopAndWording(unittest.TestCase):
    def test_the_pause_gesture_stops_a_desktop_task(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from flippy import daemon
        f = SimpleNamespace(action_tools=object(), dismiss=Mock(), play=None)
        with patch.object(daemon, "event"), patch.object(daemon, "log"):
            daemon.Flippy.pause_toggle(f)
        f.dismiss.assert_called_once()

    def test_the_stop_hint_is_in_words(self):
        from types import SimpleNamespace
        from flippy import daemon
        f = SimpleNamespace(ui=SimpleNamespace(key_label=lambda name: {"pause": "double-tap ⌘"}[name]))
        self.assertEqual(daemon.Flippy._stop_hint(f), "double-tap Cmd")
        f = SimpleNamespace(ui=SimpleNamespace(key_label=lambda name: None if name == "pause" else "⇧⌘Space"))
        self.assertEqual(daemon.Flippy._stop_hint(f), "Shift Cmd Space")  # pause off: the ask hotkey

    def test_tasks_run_at_their_own_effort_setting(self):
        import asyncio
        from unittest.mock import patch
        from flippy import brain, settings
        seen = {}

        class Client:
            def __init__(self, options):
                seen["effort"] = options.effort

            async def __aenter__(self):
                raise RuntimeError("stop")

            async def __aexit__(self, *a):
                return False
        b = brain.Brain()
        tools = DesktopTools(None, None, None, catalog=BACKGROUND_CATALOG, prompt=BACKGROUND_PROMPT)
        b.options.effort = "high"  # the answers' effort doesn't apply
        for setting in ("low", "medium", "high", "max"):
            with patch.object(settings, "_data", {**settings._data, "act": {**settings._data["act"], "effort": setting}}), \
                    patch.object(brain, "ClaudeSDKClient", Client), self.assertRaises(RuntimeError):
                asyncio.run(b.act("x", tools))
            self.assertEqual(seen["effort"], setting)


class StopFreesFlippy(unittest.TestCase):
    def _flippy(self, mode):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from flippy.requests import Request
        req = Request(1, mode)
        req.future = SimpleNamespace(cancel=Mock())
        tools = SimpleNamespace(stop=Mock())
        f = SimpleNamespace(followup_token=None, request=req, action_tools=tools, action_future=object(), busy=True,
                            gen=1, box=Mock(), ui=SimpleNamespace(action_card=Mock()), marked=False,
                            video=SimpleNamespace(recording=False), overlay=SimpleNamespace(drawing=False, clear=Mock()),
                            _stop_playback=Mock(), _cancel_fade=Mock())
        return f, req, tools

    def test_stopping_a_task_frees_flippy_right_away(self):
        from flippy import daemon
        f, req, tools = self._flippy("action")
        daemon.Flippy.dismiss(f)
        tools.stop.assert_called_once()
        self.assertTrue(req.canceled.is_set())
        self.assertFalse(f.busy)              # a new /act can start now
        self.assertIsNone(f.action_tools)

    def test_a_question_still_waits_for_its_session(self):
        from flippy import daemon
        f, req, _ = self._flippy("tutor")
        f.action_tools = None
        daemon.Flippy.dismiss(f)
        self.assertTrue(f.busy)  # cleared by the request's own finally, once the shared conversation is drained


@unittest.skipUnless(sys.platform == "darwin", "macOS apps")
class Reopened(unittest.TestCase):
    def test_a_stale_process_falls_back_to_the_app_by_bundle_id(self):
        from unittest.mock import patch
        from flippy.mac import ax
        with patch.object(ax, "running_app", return_value=None), \
                patch.object(ax, "_relaunched", return_value=4242) as relaunched, \
                patch.object(ax, "_window", return_value=None), patch.object(ax, "_menus", return_value="  (none)"):
            jpeg, size, target, elements, text = ax.look("com.spotify.client", "Spotify", 111)
        relaunched.assert_called_once_with("com.spotify.client", 111)
        self.assertEqual(target[1], 4242)       # carries on with the app's current process
        self.assertIn("no window open", text)

    def test_an_app_that_is_really_gone_still_stops_the_task(self):
        from unittest.mock import patch
        from flippy.actions import ActionError
        from flippy.mac import ax
        with patch.object(ax, "running_app", return_value=None), patch.object(ax, "_relaunched", return_value=None):
            with self.assertRaises(ActionError):
                ax.look("com.example.app", "Example", 111)
