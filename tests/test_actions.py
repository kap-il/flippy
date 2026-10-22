"""Approved tools, real in-memory MCP transport, and native adapters without posting events."""
import asyncio
import base64
import sys
import unittest
from unittest.mock import patch

import anyio
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from flippy.actions import DesktopTools, MAX_ACTIONS, TOOL_CATALOG, approval_text
from flippy.frames import ScreenFrame as Screenshot
from flippy.brain import Brain, BrainError
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock


class FakeDesktop:
    def __init__(self):
        self.captures = 0
        self.inputs = []
        self.approvals = []
        self.allowed = True
        self.error = None

    async def capture(self):
        self.captures += 1
        return Screenshot(base64.b64encode(b"fixture").decode(), (1920, 1080), (1280, 720), ("dev.flippy.fixture", 1, 7, (0, 0, 1280, 720), (1, (1280, 720))))

    async def approve(self, name, args, shot):
        self.approvals.append((name, args))
        return self.allowed

    async def perform(self, name, args, shot, cancel):
        if self.error:
            raise self.error
        self.inputs.append((name, args))

    def tools(self):
        return DesktopTools(self.capture, self.approve, self.perform)


class TestTools(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.desktop = FakeDesktop()
        self.tools = self.desktop.tools()

    async def test_action_needs_a_screenshot(self):
        result = await self.tools.invoke("click", {"x": 1, "y": 2})
        self.assertTrue(result["is_error"])
        self.assertEqual(self.desktop.inputs, [])

    async def test_approved_input_returns_a_fresh_image(self):
        await self.tools.invoke("screenshot", {})
        result = await self.tools.invoke("click", {"x": 960, "y": 540, "reason": "open"})
        self.assertEqual(self.desktop.captures, 2)
        self.assertEqual(len(self.desktop.approvals), 1)
        self.assertEqual(len(self.desktop.inputs), 1)
        self.assertEqual(result["content"][1]["mimeType"], "image/jpeg")

    async def test_decline_stops_queued_calls(self):
        await self.tools.invoke("screenshot", {})
        self.desktop.allowed = False
        results = await asyncio.gather(self.tools.invoke("key", {"combo": "return", "reason": "press"}),
                                       self.tools.invoke("type", {"text": "do not type", "reason": "write"}))
        self.assertTrue(all(r["is_error"] for r in results))
        self.assertEqual(len(self.desktop.approvals), 1)
        self.assertEqual(self.desktop.inputs, [])

    async def test_bad_coordinates_are_refused_not_clamped(self):
        for x, y in ((-1, 0), (1920, 0), (0, 1080), (True, 0), (float("nan"), 0)):
            with self.subTest(x=x, y=y):
                tools = self.desktop.tools()
                await tools.invoke("screenshot", {})
                self.assertTrue((await tools.invoke("click", {"x": x, "y": y}))["is_error"])
        self.assertEqual(self.desktop.approvals, [])

    async def test_bad_keys_and_text_never_reach_approval(self):
        for name, args in (("key", {"combo": "bogus+return"}), ("type", {"text": "\0"}),
                           ("type", {"text": "x" * 161}), ("type", {"text": "\ud800"})):
            with self.subTest(name=name, args=args):
                tools = self.desktop.tools()
                await tools.invoke("screenshot", {})
                self.assertTrue((await tools.invoke(name, args))["is_error"])
        self.assertEqual(self.desktop.inputs, [])

    async def test_backend_error_does_not_expose_raw_details(self):
        await self.tools.invoke("screenshot", {})
        self.desktop.error = RuntimeError("secret path /private/example")
        result = await self.tools.invoke("key", {"combo": "return", "reason": "press"})
        self.assertTrue(result["is_error"])
        self.assertNotIn("/private", str(result))
        self.assertTrue(self.tools.cancel.is_set())

    async def test_oversized_escaped_proposal_is_not_shown_or_performed(self):
        await self.tools.invoke("screenshot", {})
        result = await self.tools.invoke("type", {"text": "\U0001f600" * 160, "reason": "write"})
        self.assertTrue(result["is_error"])
        self.assertEqual(self.desktop.approvals, [])
        self.assertEqual(self.desktop.inputs, [])

    async def test_action_limit(self):
        await self.tools.invoke("screenshot", {})
        for _ in range(MAX_ACTIONS):
            result = await self.tools.invoke("key", {"combo": "tab", "reason": "advance"})
            self.assertNotIn("is_error", result)
        self.assertTrue((await self.tools.invoke("key", {"combo": "tab", "reason": "advance"}))["is_error"])
        self.assertEqual(len(self.desktop.inputs), MAX_ACTIONS)

    async def test_cancel_during_approval_prevents_input(self):
        await self.tools.invoke("screenshot", {})

        async def approve(*args):
            self.tools.stop()
            return True
        self.tools.approve = approve
        self.assertTrue((await self.tools.invoke("key", {"combo": "return", "reason": "press"}))["is_error"])
        self.assertEqual(self.desktop.inputs, [])

    async def test_coroutine_cancellation_stops_the_native_worker(self):
        await self.tools.invoke("screenshot", {})
        started = asyncio.Event()

        async def perform(*args):
            started.set()
            await asyncio.Event().wait()
        self.tools.perform = perform
        task = asyncio.create_task(self.tools.invoke("type", {"text": "test", "reason": "write"}))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.tools.cancel.is_set())

    async def test_cleanup_failure_is_retained_for_controller(self):
        from flippy.actions import InputCleanupError
        await self.tools.invoke("screenshot", {})
        self.desktop.error = InputCleanupError("Release failed.")
        result = await self.tools.invoke("key", {"combo": "return", "reason": "press"})
        self.assertTrue(result["is_error"])
        self.assertTrue(self.tools.cleanup_failed)
        self.assertTrue(self.tools.cancel.is_set())

    async def test_acting_before_a_screenshot_is_refused_without_ending_the_task(self):
        result = await self.tools.invoke("key", {"combo": "return", "reason": "press"})
        self.assertTrue(result["is_error"])
        self.assertFalse(self.tools.cancel.is_set())
        self.assertIsNone(self.tools.failure)
        self.assertEqual(self.tools.actions, 0)
        await self.tools.invoke("screenshot", {})  # then it looks, and can go on
        result = await self.tools.invoke("key", {"combo": "return", "reason": "press"})
        self.assertNotIn("is_error", result)
        self.assertEqual(self.tools.actions, 1)

    async def test_held_input_is_its_own_reason_not_a_cleanup_failure(self):
        from flippy.actions import InputHeldError
        await self.tools.invoke("screenshot", {})
        self.desktop.error = InputHeldError("Let go of the keyboard and mouse while Flippy acts.")
        result = await self.tools.invoke("key", {"combo": "return", "reason": "press"})
        self.assertTrue(result["is_error"])
        self.assertEqual(self.tools.failure_code, "input_held")
        self.assertFalse(self.tools.cleanup_failed)  # nothing was pressed, so nothing needs a restart

    async def test_bounded_scroll_and_drag(self):
        await self.tools.invoke("screenshot", {})
        for name, args in (("scroll", {"x": 30, "y": 40, "direction": "left", "lines": 10, "reason": "pan"}),
                           ("drag", {"x": 1, "y": 2, "to_x": 100, "to_y": 200, "modifiers": [], "reason": "move"})):
            self.assertNotIn("is_error", await self.tools.invoke(name, args))
        self.assertEqual(self.tools.actions, 2)
        self.assertEqual(self.desktop.captures, 3)

    async def test_gesture_validation_before_approval(self):
        scroll = {"x": 1, "y": 2, "direction": "down", "lines": 1, "reason": "scroll"}
        drag = {"x": 1, "y": 2, "to_x": 3, "to_y": 4, "modifiers": [], "reason": "drag"}
        cases = [("scroll", {**scroll, "lines": n}) for n in (0, 11, True, 1.2)]
        cases += [("scroll", {**scroll, "direction": "diagonal"}),
                  ("drag", {**drag, "to_x": 1, "to_y": 2}),
                  ("drag", {**drag, "modifiers": ["cmd", "cmd"]}),
                  ("drag", {**drag, "modifiers": ["alt"]}),
                  ("drag", {**drag, "to_x": 1920}),
                  ("drag", {**drag, "extra": 1}),
                  ("drag", {**drag, "reason": ""}),
                  ("drag", {k: v for k, v in drag.items() if k != "modifiers"})]
        for name, args in cases:
            with self.subTest(args=args):
                tools = self.desktop.tools()
                await tools.invoke("screenshot", {})
                self.assertTrue((await tools.invoke(name, args))["is_error"])
        self.assertEqual(self.desktop.approvals, [])

    async def test_mcp_schema_images_and_refusals_over_real_transport(self):
        server = self.tools.server()["instance"]
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            async with anyio.create_task_group() as group:
                group.start_soon(server.run, *server_streams, server.create_initialization_options())
                async with ClientSession(*client_streams) as client:
                    await client.initialize()
                    listed = await client.list_tools()
                    self.assertEqual({t.name for t in listed.tools}, set(TOOL_CATALOG))
                    invalid = await client.call_tool("click", {"x": 1, "y": 2})  # reason required
                    self.assertTrue(invalid.model_dump(by_alias=True)["isError"])
                    result = await client.call_tool("screenshot", {})
                    self.assertFalse(result.model_dump(by_alias=True)["isError"])
                    self.assertEqual(result.content[1].type, "image")
                    result = await client.call_tool("click", {"x": 20, "y": 20, "reason": "open"})
                    self.assertFalse(result.model_dump(by_alias=True)["isError"])
                    self.desktop.allowed = False
                    result = await client.call_tool("type", {"text": "hello", "reason": "write"})
                    self.assertTrue(result.model_dump(by_alias=True)["isError"])
                    self.assertEqual(len(self.desktop.inputs), 1)
                group.cancel_scope.cancel()


class TestApproval(unittest.TestCase):
    def test_invisible_characters_are_shown_literally(self):
        shown = approval_text("type", {"text": "hello\n\u202etest"})
        self.assertIn("hello\\n\\u202etest", shown)
        self.assertNotIn("\u202e", shown)


class TestActionSession(unittest.IsolatedAsyncioTestCase):
    async def run_session(self, tools, *, error=False, said=()):
        options = []

        class Client:
            def __init__(self, *, options):
                self.options = options

            async def __aenter__(self):
                options.append(self.options)
                return self

            async def __aexit__(self, *args):
                pass

            async def query(self, question):
                self.question = question

            async def receive_response(self):
                for text in said:
                    yield AssistantMessage(content=[TextBlock(text)], model="fixture")
                yield ResultMessage(subtype="error_max_turns" if error else "success",
                                    duration_ms=1, duration_api_ms=1, is_error=error,
                                    num_turns=1, session_id="fixture", result="done")

        brain = Brain()
        brain.configure("sonnet", "medium")
        with patch("flippy.brain.ClaudeSDKClient", Client):
            result = await brain.act("do the task", tools)
        return brain, options[0], result

    async def test_what_claude_says_between_steps_reaches_the_acting_card(self):
        tools = FakeDesktop().tools()
        thoughts = []
        tools.on_thought = thoughts.append
        await self.run_session(tools, said=("Spotify is open; looking for search.", "  "))
        self.assertEqual(thoughts, ["Spotify is open; looking for search."])  # blank text isn't a thought

    async def test_action_options_preserve_model_and_keep_tutor_tool_free(self):
        brain, options, result = await self.run_session(FakeDesktop().tools())
        self.assertEqual(result, "done")
        from flippy import settings
        self.assertEqual((options.model, options.effort), (brain.options.model, settings.get("act", "effort")))
        self.assertEqual(options.tools, [])
        self.assertEqual(options.setting_sources, [])
        self.assertTrue(options.strict_mcp_config)
        self.assertEqual(set(options.allowed_tools),
                         {f"mcp__desktop__{n}" for n in TOOL_CATALOG})
        self.assertEqual(brain.options.mcp_servers, {})
        self.assertEqual(brain.options.allowed_tools, [])
        self.assertEqual(brain.options.max_turns, 1)

    async def test_stopped_tools_override_model_success(self):
        tools = FakeDesktop().tools()
        tools.failure = "Action declined."
        tools.stop()
        _, _, result = await self.run_session(tools)
        self.assertIn("Task stopped", result)
        self.assertIn("Action declined", result)

    async def test_turn_limit_is_not_reported_as_success(self):
        with self.assertRaises(BrainError):
            await self.run_session(FakeDesktop().tools(), error=True)


@unittest.skipUnless(sys.platform == "darwin", "macOS controller")
class TestActionController(unittest.IsolatedAsyncioTestCase):
    async def test_action_command_does_not_log_task_text(self):
        from flippy.daemon import Flippy
        controller = Flippy.__new__(Flippy)
        controller.act = unittest.mock.Mock(return_value="started")
        with patch("flippy.daemon.log") as log:
            self.assertEqual(controller.command("act type private fixture text"), "started")
        self.assertNotIn("private fixture", str(log.call_args_list))
        controller.act.assert_called_once_with("type private fixture text")

    async def test_canceled_ui_operation_never_opens_approval(self):
        from flippy.daemon import Flippy
        queued = []
        fn = unittest.mock.Mock()
        controller = Flippy.__new__(Flippy)
        with patch("flippy.daemon.loop.idle_add", side_effect=queued.append):
            task = asyncio.create_task(controller._action_main(fn))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            queued[0]()
        fn.assert_not_called()


@unittest.skipUnless(sys.platform == "darwin", "macOS only")
class TestNativeAdapter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from flippy.mac import ui
        cls.module = ui

    def setUp(self):
        for name in ("CGEventSourceKeyState", "CGEventSourceButtonState"):
            mock = patch.object(self.module.Quartz, name, return_value=False)
            mock.start()
            self.addCleanup(mock.stop)
        self.ui = self.module.Platform.__new__(self.module.Platform)

    def test_typing_waits_for_all_characters(self):
        posted = []
        with patch.object(self.ui, "_can_post", return_value=None), \
                patch.object(self.module.Quartz, "CGEventCreateKeyboardEvent", return_value=object()), \
                patch.object(self.module.Quartz, "CGEventSetFlags"), \
                patch.object(self.module.Quartz, "CGEventKeyboardSetUnicodeString"), \
                patch.object(self.module.Quartz, "CGEventPost"), patch.object(self.module.time, "sleep"):
            self.assertEqual(self.ui.type_text("abc", on_key=posted.append, wait=True), "ok")
            self.assertEqual(posted, ["a", "b", "c"])



if __name__ == "__main__":
    unittest.main()
