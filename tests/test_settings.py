"""Settings migration (flippy/settings.py)."""
import unittest
import copy
import contextlib
import io
import os
import sys
from types import SimpleNamespace
import tempfile
import tomllib
from unittest.mock import patch
from pathlib import Path

from flippy import settings


class TestMigrate(unittest.TestCase):
    def test_v1_theme_names(self):
        self.assertEqual(settings._migrate({"look": {"theme": "glass"}})["look"]["theme"], "mediaplayer")
        self.assertEqual(settings._migrate({"look": {"theme": "nowplaying"}})["look"]["theme"], "glass")
        self.assertEqual(settings._migrate({"look": {"theme": "y2k"}})["look"]["theme"], "y2k")

    def test_v1_shine_setting_moves(self):
        look = settings._migrate({"look": {"glass_shine": "none"}})["look"]
        self.assertEqual(look, {"player_shine": "none"})

    def test_v2_files_are_left_alone(self):
        self.assertEqual(settings._migrate({"version": 2, "look": {"theme": "glass"}})["look"]["theme"], "glass")


class TestSettings(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "config.toml")
        self.original = copy.deepcopy(settings._data)
        self.listeners = settings._listeners[:]
        self.path_patch = patch.object(settings, "PATH", self.path)
        self.path_patch.start()
        settings._listeners = []
        settings.load()

    def tearDown(self):
        settings._data = self.original
        settings._listeners = self.listeners
        self.path_patch.stop()
        self.temp.cleanup()

    def test_upstream_onboarding_setting_remains_available(self):
        self.assertFalse(settings.get("onboarding", "done"))
        settings.set("onboarding", "done", True)
        settings.load()
        self.assertTrue(settings.get("onboarding", "done"))

    def test_action_and_provider_defaults_are_explicit(self):
        self.assertEqual(settings.get("provider", "mode"), "auto")
        self.assertEqual(settings.get("act", "mode"), "per_app")
        self.assertEqual(settings.get("act", "allowed_apps"), [])
        self.assertEqual(settings.get("codex", "model"), "default")
        self.assertEqual(settings.get("codex", "effort"), "medium")
        for mode in ("per_app", "auto", "every_input"):
            settings.set("act", "mode", mode)
            self.assertEqual(settings.action_limits(), settings.ACT_LIMITS[mode])
        strict = settings.ACT_LIMITS["every_input"]
        for mode in ("per_app", "auto"):
            limits = settings.ACT_LIMITS[mode]
            self.assertGreater(limits.max_actions, strict.max_actions)
            self.assertGreater(limits.max_text, strict.max_text)
            self.assertGreater(limits.timeout_seconds, strict.timeout_seconds)
            self.assertGreater(limits.max_turns, strict.max_turns)
        with self.assertRaises(ValueError):
            settings.set("act", "mode", "unknown")
        with self.assertRaises(ValueError):
            settings.set("provider", "mode", "api")

    def test_allowed_apps_array_round_trip_and_revocation(self):
        apps = ["com.apple.TextEdit", "org.mozilla.firefox"]
        settings.set("act", "allowed_apps", apps)
        apps.append("com.example.external-mutation")
        exposed = settings.get("act", "allowed_apps")
        exposed.append("com.example.get-mutation")
        self.assertEqual(settings.get("act", "allowed_apps"), ["com.apple.TextEdit", "org.mozilla.firefox"])
        settings.load()
        self.assertEqual(settings.get_list("act", "allowed_apps"), {"com.apple.TextEdit", "org.mozilla.firefox"})
        settings.remove_allowed_app("com.apple.TextEdit")
        settings.load()
        self.assertEqual(settings.get("act", "allowed_apps"), ["org.mozilla.firefox"])
        settings.remove_allowed_app("not.in.list")
        self.assertEqual(settings.get("act", "allowed_apps"), ["org.mozilla.firefox"])
        settings.set_list("act", "allowed_apps", {"com.apple.TextEdit", "org.mozilla.firefox"})
        self.assertEqual(settings.get("act", "allowed_apps"), ["com.apple.TextEdit", "org.mozilla.firefox"])

    def test_allowed_apps_invalid_values_are_refused(self):
        for value in ("com.apple.TextEdit", ["com.apple.TextEdit", "com.apple.TextEdit"],
                      [""], ["*"], ["com.*"], ["single"], ["com.apple. TextEdit"],
                      ["com.apple.\nTextEdit"], ["com.apple/evil"], ["com.apple.☃"],
                      [True], [None], [["com.apple.TextEdit"]],
                      ["com.example." + "a" * 256],
                      [f"com.example.app{i}" for i in range(129)]):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    settings.set("act", "allowed_apps", value)
        self.assertEqual(settings.get("act", "allowed_apps"), [])
        self.assertEqual(settings.parse_value("act.allowed_apps", '["com.apple.TextEdit"]'), ["com.apple.TextEdit"])
        for raw in ('[true]', '["com.*"]', 'not-an-array', '["com.a", "com.a"]'):
            with self.assertRaises(ValueError):
                settings.parse_value("act.allowed_apps", raw)

    def test_permission_lists_cannot_be_mutated_through_listener(self):
        settings.on_change(lambda _s, _k, value: value.append("com.example.unapproved") if isinstance(value, list) else None)
        settings.set("act", "allowed_apps", ["com.apple.TextEdit"])
        self.assertEqual(settings.get("act", "allowed_apps"), ["com.apple.TextEdit"])

    def test_codex_model_and_effort_are_independent(self):
        settings.set("codex", "model", "gpt-5.4")
        settings.set("codex", "effort", "xhigh")
        settings.set("provider", "mode", "codex")
        settings.load()
        self.assertEqual(settings.get("claude", "model"), "default")
        self.assertEqual(settings.get("claude", "effort"), "low")
        self.assertEqual(settings.get("codex", "model"), "gpt-5.4")
        self.assertEqual(settings.get("codex", "effort"), "xhigh")
        for value in ("", " model", "model\n", "model/key", "☃", "a" * 129):
            with self.assertRaises(ValueError):
                settings.set("codex", "model", value)
        with self.assertRaises(ValueError):
            settings.set("codex", "effort", "max")

    def test_all_defaults_valid_and_numeric_boundaries(self):
        for (section, key), spec in settings.SETTINGS.items():
            self.assertTrue(settings._valid(section, key, spec.default), (section, key))
            if spec.minimum is not None:
                self.assertTrue(settings._valid(section, key, spec.minimum))
                self.assertTrue(settings._valid(section, key, spec.maximum))
                self.assertFalse(settings._valid(section, key, spec.minimum - 1))
                self.assertFalse(settings._valid(section, key, spec.maximum + 1))
                for value in (True, False, float("nan"), float("inf"), -float("inf")):
                    self.assertFalse(settings._valid(section, key, value))

    def test_invalid_writes_leave_disk_memory_and_listeners_untouched(self):
        changed = []
        settings.on_change(lambda *args: changed.append(args))
        for section, key, value in (("look", "text_size", 12.5), ("claude", "model", "other"),
                                    ("updates", "check", 1), ("look", "glass_color", "unknown"),
                                    ("look", "missing", "x"), ("missing", "x", "x")):
            before = copy.deepcopy(settings._data)
            with self.assertRaises(ValueError):
                settings.set(section, key, value)
            self.assertEqual(settings._data, before)
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(changed, [])

    def test_failed_save_does_not_publish_and_removes_temporary_file(self):
        settings.save()
        before = Path(self.path).read_bytes()
        with patch.object(settings.os, "replace", side_effect=OSError("failure")):
            with self.assertRaises(OSError):
                settings.set("timing", "speed", 1.5)
        self.assertEqual(settings.get("timing", "speed"), 1.0)
        self.assertEqual(Path(self.path).read_bytes(), before)
        self.assertEqual(os.listdir(self.temp.name), ["config.toml"])

    def test_listener_sees_persisted_value_once(self):
        changed = []
        def listener(section, key, value):
            with open(self.path, "rb") as stream:
                self.assertEqual(tomllib.load(stream)[section][key], value)
            self.assertEqual(settings.get(section, key), value)
            changed.append(value)
        settings.on_change(listener)
        settings.set("timing", "speed", 1.23456)
        settings.set("timing", "speed", 1.23456)
        self.assertEqual(changed, [1.23456])
        settings.load()
        self.assertEqual(settings.get("timing", "speed"), 1.23456)

    def test_string_round_trip(self):
        value = 'unicode é ☃ " slash\\ newline\n tab\t return\r nul\0 delete\x7f'
        settings.set("help", "apps", value)
        settings.load()
        self.assertEqual(settings.get("help", "apps"), value)

    def test_load_invalid_fields_individually_without_logging_content(self):
        Path(self.path).write_text('version = 2\n[look]\ntheme = "private-invalid-value"\ntext_size = 12.5\ncard_opacity = 0.8\n[timing]\nspeed = nan\n')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            settings.load()
        self.assertNotIn("private-invalid-value", output.getvalue())
        self.assertEqual(settings.get("look", "theme"), "mono")
        self.assertEqual(settings.get("look", "text_size"), 15)
        self.assertEqual(settings.get("look", "card_opacity"), .8)
        self.assertEqual(settings.get("timing", "speed"), 1.)

    def test_malformed_file_and_legacy_bad_theme_do_not_crash(self):
        for content in ('[broken content', '[look]\ntheme = ["bad"]\n'):
            Path(self.path).write_text(content)
            with contextlib.redirect_stdout(io.StringIO()):
                settings.load()
            self.assertEqual(settings.get("look", "theme"), "mono")

    def test_legacy_settings_and_custom_pointers_remain_supported(self):
        Path(self.path).write_text('[look]\ntheme = "glass"\nglass_shine = "none"\n[claude]\nmodel = "sonnet"\neffort = "high"\nimage = 1366\n')
        settings.load()
        self.assertEqual(settings.get("look", "theme"), "mediaplayer")
        self.assertEqual(settings.get("look", "player_shine"), "none")
        self.assertEqual(settings.get("claude", "model"), "sonnet")
        self.assertEqual(settings.get("claude", "effort"), "high")
        self.assertEqual(settings.get("claude", "image"), 1366)
        with patch("flippy.pointers.exists", return_value=True):
            settings.set("look", "pointer", "custom:example")
            settings.load()
            self.assertEqual(settings.get("look", "pointer"), "custom:example")
        with patch("flippy.pointers.exists", return_value=False):
            self.assertFalse(settings._valid("look", "pointer", "custom:missing"))

    def test_cli_parsing_and_programmatic_rules_agree(self):
        for key, raw, value in (("updates.check", "false", False), ("updates.check", "TRUE", True),
                                ("look.text_size", "12", 12), ("timing.speed", "1.25", 1.25),
                                ("claude.image", "0", 0)):
            self.assertEqual(settings.parse_value(key, raw), value)
        for key, raw in (("updates.check", "yes"), ("updates.check", "typo"),
                         ("look.text_size", "12.5"), ("look.text_size", "12.0"),
                         ("timing.speed", "nan"), ("timing.speed", "0"),
                         ("claude.model", "other"), ("unknown.key", "x")):
            with self.assertRaises(ValueError):
                settings.parse_value(key, raw)

    def test_choice_metadata_matches_compatibility_table(self):
        for key, spec in settings.SETTINGS.items():
            if spec.choices:
                self.assertEqual([v for v, _ in settings.options(*key)], settings.CHOICES[key])


class TestAllowedAppRemoval(unittest.TestCase):
    """The app window removes one allowed app by writing the list without it (flippy/shell/bridge.py)."""
    def test_removing_one_keeps_the_others(self):
        from flippy.shell import bridge
        values = ["com.apple.TextEdit", "org.mozilla.firefox"]
        with patch("flippy.shell.bridge.settings.set") as write, patch("flippy.shell.bridge.schema.field", return_value={}):
            bridge.Bridge().op_set("act.allowed_apps", values[:1])
        write.assert_called_once_with("act", "allowed_apps", ["com.apple.TextEdit"])


if __name__ == "__main__":
    unittest.main()
