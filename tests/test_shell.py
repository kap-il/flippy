"""The app window's page model and bridge (flippy/shell): plain Python, no webview."""
import os
import tempfile
import unittest
from unittest import mock

from flippy import settings
from flippy.shell import bridge, schema


class ShellTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(settings, "PATH", os.path.join(self.tmp.name, "config.toml"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        settings.load()


class SchemaTest(ShellTestCase):
    def test_every_visible_setting_is_on_some_page(self):
        for plat in ("darwin", "linux"):
            shown = {f["id"] for p in schema.pages(plat) for f in p["fields"]}
            for (section, key) in settings.SETTINGS:
                if (section, key) in schema.HIDDEN:
                    self.assertNotIn(f"{section}.{key}", shown)
                    continue
                platforms = schema.LABELS.get((section, key), (None, None, None))[2]
                self.assertEqual(f"{section}.{key}" in shown, not platforms or plat in platforms, (plat, section, key))

    def test_new_settings_show_up_without_window_work(self):
        extra = dict(settings.SETTINGS)
        extra["look", "corner"] = settings.Setting("top", (("top", "Top"), ("corner", "Corner")))
        extra["recall", "days"] = settings.Setting(30, (), 1, 365, 1, 0)
        defaults = {s: dict(v) for s, v in settings.DEFAULTS.items()}
        defaults["look"]["corner"] = "top"
        defaults["recall"] = {"days": 30}
        with mock.patch.object(settings, "SETTINGS", extra), mock.patch.object(settings, "DEFAULTS", defaults), \
                mock.patch.object(settings, "_data", defaults):
            pages = {p["id"]: p for p in schema.pages("darwin")}
        corner = next(f for f in pages["appearance"]["fields"] if f["id"] == "look.corner")
        self.assertEqual((corner["label"], corner["control"]), ("Corner", "choice"))
        self.assertEqual(pages["recall"]["fields"][0]["control"], "slider")  # a section no page claims

    def test_controls(self):
        self.assertEqual(schema.control("look", "theme"), "choice")
        self.assertEqual(schema.control("act", "infer"), "switch")
        self.assertEqual(schema.control("look", "text_size"), "slider")
        self.assertEqual(schema.control("act", "allowed_apps"), "list")
        self.assertEqual(schema.control("keys", "ask"), "shortcut")
        self.assertEqual(schema.control("help", "apps"), "text")


class BridgeTest(ShellTestCase):
    def setUp(self):
        super().setUp()
        self.pushed, self.commands = [], []
        self.b = bridge.Bridge(push=lambda e, d: self.pushed.append((e, d)), command=self.commands.append)
        self.addCleanup(self.b.close)

    def test_state(self):
        self.b.route = "settings/timing"
        r = self.b.handle({"id": 1, "op": "state"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["result"]["route"], "settings/timing")
        self.assertIn("appearance", [p["id"] for p in r["result"]["pages"]])

    def test_set_saves_validates_and_pushes(self):
        r = self.b.handle({"id": 2, "op": "set", "args": {"id": "look.text_size", "value": 18}})
        self.assertTrue(r["ok"])
        self.assertEqual(settings.get("look", "text_size"), 18)
        self.assertEqual(self.pushed[-1][0], "setting")
        self.assertEqual(self.pushed[-1][1]["value"], 18)
        bad = self.b.handle({"id": 3, "op": "set", "args": {"id": "look.text_size", "value": 999}})
        self.assertFalse(bad["ok"])
        self.assertEqual(settings.get("look", "text_size"), 18)

    def test_refuses_unknown_hidden_and_malformed(self):
        for message in [{"id": 4, "op": "set", "args": {"id": "onboarding.done", "value": True}},
                        {"id": 5, "op": "set", "args": {"id": "nope.nothing", "value": 1}},
                        {"id": 6, "op": "eval", "args": {"code": "1"}},
                        {"id": 7, "op": "_changed"},
                        {"id": 8, "op": "state", "args": ["x"]},
                        {"id": 9, "op": "set", "args": {"id": "look.theme", "value": "mono", "extra": 1}},
                        "not a dict", None]:
            self.assertFalse(self.b.handle(message)["ok"], message)
        self.assertFalse(settings.get("onboarding", "done"))

    def test_commands_are_allowlisted(self):
        self.assertTrue(self.b.handle({"id": 10, "op": "command", "args": {"name": "preview"}})["ok"])
        self.assertFalse(self.b.handle({"id": 11, "op": "command", "args": {"name": "quit"}})["ok"])
        self.assertEqual(self.commands, ["preview"])

    def test_an_unexpected_error_still_answers_the_page(self):
        logged = []
        self.b.log = logged.append
        with mock.patch.object(bridge.appearance, "themes_gallery", side_effect=RuntimeError("cairo broke")):
            r = self.b.handle({"id": 12, "op": "gallery"})
        self.assertEqual(r, {"id": 12, "ok": False, "error": "Something went wrong (RuntimeError)."})
        self.assertIn("RuntimeError", logged[-1])
        bad = self.b.handle({"id": 13, "op": "set", "args": {"id": "look.text_size", "value": 999}})
        self.assertEqual(bad["error"], "invalid setting look.text_size")  # expected errors keep their words

    def test_close_stops_pushing(self):
        self.b.close()
        settings.set("look", "text_size", 20)
        self.assertEqual(self.pushed, [])


if __name__ == "__main__":
    unittest.main()


class AppearanceTest(ShellTestCase):
    """The Appearance page's Python side: studio saves, pointer files, looks (flippy/shell/appearance.py)."""
    def setUp(self):
        super().setUp()
        from flippy import pointers
        d = os.path.join(self.tmp.name, "pointers")
        for name, value in (("DIR", d), ("INDEX", os.path.join(d, "index.json"))):
            p = mock.patch.object(pointers, name, value)
            p.start()
            self.addCleanup(p.stop)
        pointers._cache.clear()
        self.pointers = pointers
        self.pushed = []
        self.b = bridge.Bridge(push=lambda e, d: self.pushed.append((e, d)))
        self.addCleanup(self.b.close)

    def call(self, op, **args):
        r = self.b.handle({"id": 1, "op": op, "args": args})
        if not r["ok"]:
            raise ValueError(r["error"])
        return r["result"]

    def _pixel_hand(self, name="Hand copy"):
        t = self.call("studio_template", kind="hand")
        return self.call("studio_save", mode="pixel", name=name, hotspot=t["hotspot"], cells=t["cells"])

    def test_studio_pixel_round_trip(self):
        value = self._pixel_hand()
        self.assertEqual(value, "custom:Hand copy")
        self.assertEqual(settings.get("look", "pointer"), value)
        opened = self.call("studio_open", name="Hand copy")
        self.assertEqual(opened["mode"], "pixel")
        self.assertEqual(opened["cells"], self.call("studio_template", kind="hand")["cells"])

    def test_studio_smooth_and_image(self):
        import base64
        import io
        from PIL import Image
        from flippy import pixelart
        buf = io.BytesIO()
        Image.new("RGBA", (pixelart.GRID_W * pixelart.RES, pixelart.GRID_H * pixelart.RES), (255, 0, 0, 255)).save(buf, "PNG")
        url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        self.call("studio_save", mode="smooth", name="Red", hotspot=[3, 4], image=url)
        self.assertEqual(self.call("studio_open", name="Red")["mode"], "smooth")
        self.assertEqual(self.call("studio_open", name="Red")["hotspot"], [3, 4])
        with self.assertRaises(ValueError):  # wrong canvas size
            self.call("studio_save", mode="smooth", name="Bad", hotspot=[0, 0], image=self._png(10, 10))
        self.call("studio_save", mode="image", name="Photo", hotspot=[5, 6], image=self._png(100, 80), crisp=False)
        opened = self.call("studio_open", name="Photo")
        self.assertEqual((opened["mode"], opened["hotspot"], opened["size"]), ("image", [5, 6], [100, 80]))
        with self.assertRaises(ValueError):  # tip outside the image
            self.call("studio_save", mode="image", name="Photo", hotspot=[500, 0], image=self._png(100, 80))

    def _png(self, w, h):
        import base64
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGBA", (w, h), (0, 0, 255, 255)).save(buf, "PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    def test_studio_refuses_bad_input(self):
        t = self.call("studio_template", kind="hand")
        for bad in [dict(mode="pixel", name="x", hotspot=[99, 0], cells=t["cells"]),
                    dict(mode="pixel", name="x", hotspot=[0, 0], cells=t["cells"][:3]),
                    dict(mode="pixel", name="x", hotspot=[0, 0], cells=[["#zzzzzz"] * 20] * 24),
                    dict(mode="paint", name="x", hotspot=[0, 0]),
                    dict(mode="smooth", name="x", hotspot=[0, 0], image="javascript:alert(1)")]:
            with self.assertRaises(ValueError):
                self.call("studio_save", **bad)
        self.assertEqual(self.pointers.names(), [])

    def test_names_are_cleaned(self):
        self._pixel_hand("../../etc/passwd")
        self.assertEqual(self.pointers.names(), ["etcpasswd"])

    def test_rename_duplicate_delete_keep_the_setting_right(self):
        self._pixel_hand("A")
        self.assertEqual(self.call("pointer_rename", name="A", new="B"), "custom:B")
        self.assertEqual(settings.get("look", "pointer"), "custom:B")
        self.assertEqual(self.call("pointer_duplicate", name="B"), "custom:B copy")
        self.call("pointer_delete", name="B")
        self.assertEqual(settings.get("look", "pointer"), "theme")
        self.assertEqual(self.pointers.names(), ["B copy"])

    def test_exported_png_carries_its_tip(self):
        from flippy.shell import appearance
        self._pixel_hand("Tip")
        data = appearance.export_png("Tip")
        self.pointers.delete("Tip")
        r = appearance.import_file(data, "Tip.png")
        self.assertEqual(r["saved"], "custom:Tip")
        self.assertEqual(self.pointers.meta("Tip")["hotspot"], list(self.call("studio_template", kind="hand")["hotspot"]))
        plain = appearance.import_file(__import__("base64").b64decode(self._png(300, 200).split(",")[1]), "photo.png")
        self.assertEqual(plain["studio"]["mode"], "image")
        self.assertLessEqual(max(plain["studio"]["size"]), self.pointers.MAX_IMPORT_PX)

    def test_look_copy_and_import(self):
        from flippy.shell import appearance
        self._pixel_hand("Mine")
        settings.set("look", "theme", "glass")
        text = self.call("look_copy")["text"]
        settings.set("look", "theme", "mono")
        settings.set("look", "pointer", "theme")
        self.pointers.delete("Mine")
        r = self.call("look_import", text=text)
        self.assertEqual(settings.get("look", "theme"), "glass")
        self.assertEqual(settings.get("look", "pointer"), "custom:Mine")
        self.assertEqual(r["before"]["theme"], "mono")
        partial = appearance.apply_look('{"format": "flippy-look/1", "theme": "nope", "text_size": 18, "evil": 1}')
        self.assertEqual(settings.get("look", "text_size"), 18)
        self.assertEqual(sorted(partial["skipped"]), ["evil", "theme"])
        for bad in ["not json", '{"theme": "glass"}', "[]"]:
            with self.assertRaises(ValueError):
                self.call("look_import", text=bad)

    def test_set_many_is_all_or_nothing_and_reset_returns_old_values(self):
        with self.assertRaises(ValueError):
            self.call("set_many", values={"look.text_size": 18, "look.theme": "nope"})
        self.assertEqual(settings.get("look", "text_size"), settings.DEFAULTS["look"]["text_size"])
        self.call("set_many", values={"look.text_size": 18, "window.mode": "dark"})
        before = self.call("reset", page="appearance")
        self.assertEqual((before["look.text_size"], before["window.mode"]), (18, "dark"))
        self.assertEqual(settings.get("window", "mode"), "system")

    def test_galleries(self):
        g = self.call("gallery")
        self.assertEqual([t["key"] for t in g["themes"]][:2], ["mono", "midnight"])
        self.assertIn("System", [t["name"] for t in g["themes"]])
        self.assertTrue(all(t["image"].startswith("data:image/png;base64,") for t in g["themes"]))
        self.assertEqual(g["pointers"][0]["value"], "theme")

    def test_shortcuts_need_a_modifier(self):
        with self.assertRaises(ValueError):
            self.call("set", id="keys.ask", value="space")
        if schema.platform() == "darwin":
            with self.assertRaises(ValueError):
                self.call("set", id="keys.ask", value="cmd+nosuchkey")
            self.call("set", id="keys.ask", value="ctrl+option+k")
            self.assertEqual(settings.get("keys", "ask"), "ctrl+option+k")

    def test_set_many_checks_shortcuts_like_set(self):
        with self.assertRaises(ValueError):
            self.call("set_many", values={"look.text_size": 18, "keys.ask": "space"})
        if schema.platform() == "darwin":
            with self.assertRaises(ValueError):
                self.call("set_many", values={"look.text_size": 18, "keys.ask": "cmd+nosuchkey"})  # gitleaks:allow (a hotkey, not a key)
        self.assertEqual(settings.get("look", "text_size"), settings.DEFAULTS["look"]["text_size"])


class SchemaExtrasTest(ShellTestCase):
    def test_controls_switch_and_theme_options(self):
        f = schema.field("look", "controls")
        self.assertEqual((f["control"], f["on"], f["off"]), ("switch", "all", "players"))
        self.assertEqual(schema.field("look", "player_shine", "darwin")["when"], {"id": "look.theme", "values": ["mediaplayer"]})
        self.assertIsNone(schema.field("look", "player_shine", "linux"))
        self.assertEqual(schema.field("window", "mode")["choices"][0]["value"], "system")


class LinuxPages(ShellTestCase):
    def test_cosmic_shortcuts_page_without_mac_keys(self):
        pages = {p["id"]: p for p in schema.pages("linux")}
        self.assertEqual(pages["shortcuts"]["fields"], [])  # COSMIC's own list (shortcuts.js)
        self.assertNotIn("keys.ask", [f["id"] for p in pages.values() for f in p["fields"]])
        mac = {p["id"]: p for p in schema.pages("darwin")}
        self.assertIn("keys.ask", [f["id"] for f in mac["shortcuts"]["fields"]])


class PageActions(ShellTestCase):
    """The old Settings window's buttons (fresh session, check now, config file) are on the pages, through allowed ops."""
    def test_actions_use_allowed_commands_and_ops(self):
        pages = {p["id"]: p for p in schema.pages("darwin")}
        acts = pages["general"]["actions"]
        self.assertEqual({a["args"]["name"] for a in acts if a["op"] == "command"}, {"reset", "update"})
        self.assertTrue({a["args"]["name"] for a in acts if a["op"] == "command"} <= bridge.COMMANDS)
        for a in acts:
            self.assertTrue(hasattr(bridge.Bridge, "op_" + a["op"]), a)
        self.assertIn("/new", [a["keys"] for a in pages["shortcuts"]["actions"]])
