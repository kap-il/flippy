"""The app window's Setup page: its words and state (flippy/shell/setup.py) and the macOS checks behind it."""
import os
import sys
import tempfile
import unittest
from unittest import mock

from flippy import settings
from flippy.shell import bridge, setup


class SetupWords(unittest.TestCase):
    def test_provider_note(self):
        self.assertTrue(setup.provider_note("auto", True, True)[1])          # both connected: pick one
        self.assertIn("Pick", setup.provider_note("auto", True, True)[0])
        self.assertTrue(setup.provider_note("codex", True, False)[1])        # chosen one missing
        self.assertIn("ChatGPT isn't connected", setup.provider_note("codex", True, False)[0])
        self.assertFalse(setup.provider_note("auto", True, False)[1])

    def test_ready(self):
        self.assertTrue(setup.ready("auto", True, False))
        self.assertFalse(setup.ready("auto", True, True))
        self.assertFalse(setup.ready("auto", False, False))
        self.assertTrue(setup.ready("codex", False, True))

    def test_page_state_and_bridge(self):
        class Host(bridge.Host):
            acted = []

            def setup_state(self):
                return {"claude": True, "codex": False, "screen": False, "accessibility": True}

            def setup_action(self, name):
                self.acted.append(name)
        b = bridge.Bridge(host=Host())
        self.addCleanup(b.close)
        with mock.patch.object(settings, "get", side_effect=lambda s, k: {"provider": "auto", "keys": "cmd+shift+space"}[s]):
            r = b.handle({"id": 1, "op": "setup_state"})["result"]
            self.assertTrue(r["connected"])
            self.assertFalse(r["done"])               # Screen Recording still missing
            self.assertTrue(r["mark"].startswith("data:image/png;base64,"))
            self.assertIn("claude", r["logos"])
            b.handle({"id": 2, "op": "setup_action", "args": {"name": "screen"}})
        self.assertEqual(Host.acted, ["screen"])

    def test_route_and_demo(self):
        pushed, saved = [], []
        b = bridge.Bridge(push=lambda e, d: pushed.append(e))
        self.addCleanup(b.close)
        b.handle({"id": 1, "op": "route", "args": {"route": "setup"}})
        self.assertEqual(b.current, "setup")
        b.start_pointer_demo("Sunset", saved.append)
        self.assertEqual(pushed, ["demo-pointer"])
        self.assertEqual(b.handle({"id": 2, "op": "state"})["result"]["demo"]["name"], "Sunset")
        self.assertIsNone(b.handle({"id": 3, "op": "state"})["result"]["demo"])  # read once
        b.handle({"id": 4, "op": "demo_saved", "args": {"value": "custom:Sunset"}})
        b.handle({"id": 5, "op": "demo_saved", "args": {"value": "custom:Sunset"}})
        self.assertEqual(saved, ["custom:Sunset"])


@unittest.skipUnless(sys.platform == "darwin", "macOS setup checks")
class MacSetup(unittest.TestCase):
    def setUp(self):
        from flippy.mac import setup as mac
        self.mac = mac
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_unknown_action_is_refused(self):
        with self.assertRaises(ValueError):
            self.mac.act("rm -rf")

    def test_claude_action_uses_browser_login_and_reports_start_failure(self):
        with mock.patch.object(self.mac, "logged_in", return_value=False), \
                mock.patch.object(self.mac, "claude_cli", return_value="fixture-claude"), \
                mock.patch.object(self.mac.claude_login, "start") as start, \
                mock.patch.object(self.mac.claude_login, "waiting", return_value=True), \
                mock.patch.object(self.mac, "_claude_failed", False), \
                mock.patch.object(self.mac.subprocess, "Popen") as popen:
            start.return_value = True
            self.mac.act("claude")
            start.assert_called_once_with("fixture-claude")
            self.assertTrue(self.mac.state()["claude_waiting"])
            self.assertFalse(self.mac.state()["claude_failed"])
            start.return_value = False
            self.mac.act("claude")
            self.assertTrue(self.mac.state()["claude_failed"])
            popen.assert_not_called()

    def test_connected_claude_requires_reconnect_confirmation_unless_waiting(self):
        with mock.patch.object(self.mac, "logged_in", return_value=True), \
                mock.patch.object(self.mac, "claude_cli", return_value="fixture-claude"), \
                mock.patch.object(self.mac.claude_login, "start") as start, \
                mock.patch.object(self.mac.claude_login, "waiting", return_value=False) as waiting, \
                mock.patch.object(self.mac, "confirm_claude_reconnect", return_value=False) as confirm, \
                mock.patch.object(self.mac, "_claude_failed", False):
            self.mac.act("claude")
            confirm.assert_called_once()
            start.assert_not_called()
            confirm.return_value = True
            self.mac.act("claude")
            start.assert_called_once_with("fixture-claude")
            waiting.return_value = True
            confirm.reset_mock()
            self.mac.act("claude")
            confirm.assert_not_called()
            self.assertEqual(start.call_count, 2)

    def test_permission_actions_open_only_their_panes(self):
        with mock.patch.object(self.mac.subprocess, "Popen") as popen, \
                mock.patch.object(self.mac, "CGRequestScreenCaptureAccess") as ask:
            self.mac.act("screen")
        ask.assert_called_once()
        popen.assert_called_once_with(["open", self.mac.SCREEN_PANE])

    def test_login_item_only_for_the_installed_app(self):
        agent = os.path.join(self.tmp.name, "agent.plist")
        app = os.path.join(self.tmp.name, "Flippy.app")
        os.mkdir(app)
        not_demo, demo = mock.Mock(demo=False), mock.Mock(demo=True)
        with mock.patch.object(self.mac, "AGENT", agent), mock.patch.object(setup, "current", return_value=not_demo):
            with mock.patch.object(self.mac, "APP", "/Volumes/Flippy/Flippy.app"):
                self.mac.act("login_on")
                self.assertFalse(os.path.exists(agent))  # a disk image path would open nothing at login
            with mock.patch.object(self.mac, "APP", app):
                self.mac.act("login_on")
                self.assertTrue(os.path.exists(agent))
                with mock.patch.object(self.mac.subprocess, "run"):
                    self.mac.act("login_off")
                self.assertFalse(os.path.exists(agent))
        with mock.patch.object(self.mac, "AGENT", agent), mock.patch.object(setup, "current", return_value=demo), \
                mock.patch.object(self.mac, "APP", app):
            self.mac.act("login_on")
            self.assertFalse(os.path.exists(agent))  # the demo profile never installs login items

    def test_state_shape(self):
        with mock.patch.object(self.mac, "screen_ok", return_value=True), \
                mock.patch.object(self.mac.hotkeys, "accessibility_trusted", return_value=False), \
                mock.patch.object(self.mac, "APP", None):
            s = self.mac.state()
        self.assertEqual((s["screen"], s["accessibility"], s["app"], s["login_item_allowed"]), (True, False, None, False))


if __name__ == "__main__":
    unittest.main()
