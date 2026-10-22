"""The context folder (#22): task files (flippy/context_store.py), the session summary and brief (flippy/context.py),
and when the daemon ends a session (15 minutes idle, /new, quit)."""
import ast
import asyncio
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from flippy import context, context_store, settings

DAY = 86400


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for target, name, value in ((context_store, "DIR", os.path.join(self.tmp.name, "context")),
                                    (settings, "PATH", os.path.join(self.tmp.name, "config.toml"))):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        settings.load()

    def make(self, title="Mixing the EP", apps=("Logic Pro",), where="track 3", done=("set up sidechain",), ts=None):
        return context_store.apply([{"id": None, "title": title, "apps": list(apps), "where": where,
                                     "done": list(done)}], ts=ts)[0]


class Store(Base):
    def test_a_task_round_trips_through_its_file(self):
        task = self.make()
        again = context_store.get(task["id"])
        for key in ("title", "apps", "where", "log", "updated", "compacted"):
            self.assertEqual(again[key], task[key], key)
        path = os.path.join(context_store.DIR, task["id"] + ".md")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(context_store.DIR).st_mode), 0o700)
        text = Path(path).read_text()
        self.assertTrue(text.startswith("# Mixing the EP\napps: Logic Pro\n"))
        self.assertIn("## Where you are\ntrack 3", text)

    def test_updating_overwrites_where_and_adds_to_the_log_and_apps(self):
        task = self.make()
        context_store.apply([{"id": task["id"], "title": "ignored", "apps": ["Finder"], "where": "bus routing",
                              "done": ["bounced stems"]}])
        again = context_store.get(task["id"])
        self.assertEqual(again["title"], "Mixing the EP")
        self.assertEqual(again["apps"], ["Logic Pro", "Finder"])
        self.assertEqual(again["where"], "bus routing")
        self.assertEqual([text for _, text in again["log"]], ["set up sidechain", "bounced stems"])

    def test_the_log_keeps_its_latest_lines(self):
        task = self.make(done=[])
        for i in range(context_store.LOG_LINES + 5):
            context_store.apply([{"id": task["id"], "done": [f"step {i}"]}])
        log = context_store.get(task["id"])["log"]
        self.assertEqual(len(log), context_store.LOG_LINES)
        self.assertEqual(log[-1][1], f"step {context_store.LOG_LINES + 4}")

    def test_most_recent_first_rename_delete(self):
        old = self.make("Old", ts=time.time() - 5 * DAY)
        new = self.make("New")
        self.assertEqual([t["title"] for t in context_store.tasks()], ["New", "Old"])
        context_store.rename(old["id"], "  Renamed\n task ")
        self.assertEqual(context_store.get(old["id"])["title"], "Renamed task")
        context_store.delete(new["id"])
        self.assertEqual([t["title"] for t in context_store.tasks()], ["Renamed task"])

    def test_ids_cannot_leave_the_folder(self):
        for bad in ("../config", "a/b", "", "X"):
            self.assertIsNone(context_store.get(bad))
            context_store.delete(bad)  # quietly nothing

    def test_odd_files_are_skipped(self):
        os.makedirs(context_store.DIR, exist_ok=True)
        Path(context_store.DIR, "notes.txt").write_text("hi")
        Path(context_store.DIR, "Bad Name.md").write_text("# x")
        self.make()
        self.assertEqual(len(context_store.tasks()), 1)


class Compaction(Base):
    def test_by_time_only(self):
        settings.set("context", "compact_days", 90)
        stale = self.make("Stale", done=["a", "b", "c", "d"], ts=time.time() - 91 * DAY)
        fresh = self.make("Fresh", ts=time.time() - 89 * DAY)
        self.assertEqual(context_store.compact_due(), 1)
        got = context_store.get(stale["id"])
        self.assertTrue(got["compacted"])
        self.assertEqual(got["summary"], ["b", "c", "d"])
        self.assertEqual((got["where"], got["log"]), ("", []))
        self.assertEqual(got["updated"], stale["updated"])  # still dated when it was last picked up
        self.assertIn("compacted: yes", Path(context_store.DIR, stale["id"] + ".md").read_text())
        self.assertFalse(context_store.get(fresh["id"])["compacted"])
        self.assertEqual(context_store.compact_due(), 0)

    def test_the_setting_moves_the_line(self):
        self.make(ts=time.time() - 10 * DAY)
        settings.set("context", "compact_days", 7)
        self.assertEqual(context_store.compact_due(), 1)

    def test_picking_it_up_again_makes_it_active(self):
        task = self.make(done=["sidechain on the kick"], ts=time.time() - 100 * DAY)
        context_store.compact_due()
        context_store.apply([{"id": task["id"], "where": "back at it", "done": ["new thing"]}])
        got = context_store.get(task["id"])
        self.assertFalse(got["compacted"])
        self.assertEqual([text for _, text in got["log"]], ["sidechain on the kick", "new thing"])
        self.assertEqual(got["where"], "back at it")


class Brief(Base):
    def test_index_and_the_likeliest_task_in_full(self):
        self.make("Mixing the EP", apps=["Logic Pro"], where="bus routing next")
        self.make("Home server", apps=["Terminal"], where="ssh keys done", done=["made a key"])
        text = context_store.brief("Terminal", "why won't it connect")
        self.assertIn("- Mixing the EP (Logic Pro", text)
        self.assertNotIn("- Home server (Terminal", text)  # in full instead
        self.assertIn("# Home server", text)  # its file, log and all
        self.assertIn("made a key", text)
        self.assertNotIn("# Mixing the EP", text)
        self.assertLessEqual(len(text), context_store.BRIEF_CHARS)

    def test_compacted_tasks_only_when_the_question_matches(self):
        self.make("Sidechain compression setup", done=["routed the kick to the bass compressor"],
                  ts=time.time() - 100 * DAY)
        context_store.compact_due()
        self.assertEqual(context_store.brief("Logic Pro", "what's this icon"), "")
        text = context_store.brief("Logic Pro", "how did I set up sidechain compression last time?")
        self.assertIn("# Sidechain compression setup", text)

    def test_empty_folder(self):
        self.assertEqual(context_store.brief("Finder", "anything"), "")
        self.assertEqual(context.brief("Finder", "anything"), "")

    def test_the_note_for_a_question_and_the_switches(self):
        self.make()
        self.assertTrue(context.brief("Logic Pro", "x").startswith("[Flippy's notes"))
        settings.set("context", "skip_apps", "Logic Pro")
        self.assertEqual(context.brief("logic pro", "x"), "")
        settings.set("context", "skip_apps", "")
        settings.set("context", "keep", False)
        self.assertEqual(context.brief("Logic Pro", "x"), "")


class Summary(Base):
    def test_turns_skip_apps_and_the_switch(self):
        s = context.Session()
        s.add("ask", "how do I sidechain", "Use the side chain menu.", "Logic Pro", ts=100)
        s.add("ask", "what's my password", "", "1Password")  # a default skipped app
        s.add("ask", "  ", "", "Finder")
        settings.set("context", "keep", False)
        s.add("ask", "while off", "", "Finder")
        self.assertEqual([t["question"] for t in s.take()], ["how do I sidechain"])
        self.assertEqual(s.turns, [])

    def test_the_request_has_the_index_and_the_turns(self):
        task = self.make()
        text = context.request([{"ts": time.time(), "kind": "task", "app": "Spotify", "question": "play jazz",
                                 "answer": "Playing."}])
        self.assertIn(f"id={task['id']}", text)
        self.assertIn("Spotify] they asked flippy to do: play jazz", text)
        self.assertIn("flippy: Playing.", text)

    def test_parse_keeps_known_ids_and_drops_junk(self):
        task = self.make()
        reply = ('Sure:\n```json\n{"tasks": [{"id": "%s", "where": "w", "done": ["d1","d2","d3","d4"]},'
                 '{"id": "made-up-1234", "title": "New thing", "apps": ["Notes"]}, {"id": null}, "x"]}\n```' % task["id"])
        changes = context.parse(reply)
        self.assertEqual([c["id"] for c in changes], [task["id"], None])
        self.assertEqual(changes[0]["done"], ["d1", "d2", "d3"])
        self.assertEqual(changes[1]["title"], "New thing")
        self.assertEqual(context.parse("no json here"), [])
        self.assertEqual(context.parse('{"tasks": []}'), [])

    def test_summarize_writes_tasks_and_compacts(self):
        old = self.make("Old", ts=time.time() - 200 * DAY)
        brain = SimpleNamespace(write_text=mock.AsyncMock(return_value='{"tasks": [{"id": null, "title": "Learning '
                                                                       'pivot tables", "apps": ["Numbers"], "where": "w", "done": ["d"]}]}'))
        turns = [{"ts": time.time(), "kind": "ask", "app": "Numbers", "question": "pivot?", "answer": "Here."}]
        saved = asyncio.run(context.summarize(brain, turns))
        self.assertEqual([t["title"] for t in saved], ["Learning pivot tables"])
        system, text = brain.write_text.call_args.args
        self.assertEqual(system, context.PROMPT)
        self.assertIn("pivot?", text)
        self.assertTrue(context_store.get(old["id"])["compacted"])

    def test_nothing_to_sum_up_means_no_call(self):
        brain = SimpleNamespace(write_text=mock.AsyncMock())
        self.assertEqual(asyncio.run(context.summarize(brain, [])), [])
        settings.set("context", "keep", False)
        self.assertEqual(asyncio.run(context.summarize(brain, [{"ts": 1, "kind": "ask", "app": "", "question": "q",
                                                                "answer": ""}])), [])
        brain.write_text.assert_not_called()


class Page(Base):
    """The History page's Ongoing tasks (flippy/shell/bridge.py) and the Memory settings (schema)."""
    def test_list_rename_forget(self):
        from flippy.shell import bridge
        pushed = []
        b = bridge.Bridge(push=lambda event, data: pushed.append(event))
        self.addCleanup(b.close)
        task = self.make()
        r = b.handle({"id": 1, "op": "context_list"})["result"]
        self.assertEqual(r["tasks"][0]["log"][0]["text"], "set up sidechain")
        self.assertEqual((r["keep"], r["days"]), (True, 90))
        self.assertTrue(b.handle({"id": 2, "op": "context_rename", "args": {"id": task["id"], "title": "EP"}})["ok"])
        self.assertEqual(context_store.get(task["id"])["title"], "EP")
        self.assertFalse(b.handle({"id": 3, "op": "context_rename", "args": {"id": task["id"], "title": " "}})["ok"])
        self.assertFalse(b.handle({"id": 4, "op": "context_rename", "args": {"id": "../x", "title": "y"}})["ok"])
        b.handle({"id": 5, "op": "context_delete", "args": {"id": task["id"]}})
        self.assertEqual(context_store.tasks(), [])
        self.assertIn("context", pushed)

    def test_settings_show_on_the_general_page(self):
        from flippy.shell import schema
        general = next(p for p in schema.pages("darwin") if p["id"] == "general")
        memory = next(g for g in general["groups"] if g["title"] == "Memory")
        self.assertEqual([f["id"] for f in memory["fields"]], ["context.keep", "context.compact_days", "context.skip_apps"])
        self.assertEqual(memory["fields"][1]["unit"], "days")


def daemon_methods(*names, **namespace):
    tree = ast.parse((Path(__file__).parents[1] / "flippy/daemon.py").read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Flippy")
    nodes = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<daemon>", "exec"), namespace)
    return type("Daemon", (), {name: namespace[name] for name in names})


class SessionEnds(unittest.TestCase):
    """flippy/daemon.py: the idle timer and the summary in the background."""
    def setUp(self):
        self.timers = []
        queue = SimpleNamespace(timeout_add=lambda ms, fn: self.timers.append((ms, fn)))
        self.summarize = mock.AsyncMock(return_value=[])
        cls = daemon_methods("_arm_idle", "_idle", "_end_session", "_summarize", "_summary_done",
                             asyncio=asyncio, loop=queue, IDLE_RESET_S=900, log=mock.Mock(),
                             context=SimpleNamespace(summarize=self.summarize, SUMMARY_TIMEOUT_S=90))
        self.d = cls()
        self.d.idle_gen, self.d.quitting, self.d.busy, self.d.brief_due = 0, False, False, False
        self.d.session, self.d.brain, self.d.context_future = context.Session(), object(), None
        self.d.session.turns = [{"question": "q"}]
        self.aloop = asyncio.new_event_loop()
        self.d.loop = self.aloop

    def tearDown(self):
        self.aloop.close()

    def fire(self, index=-1):
        ms, fn = self.timers[index]
        self.assertEqual(ms, 900_000)
        fn()

    def run_pending(self):
        self.aloop.run_until_complete(asyncio.sleep(0.01))

    def test_fifteen_idle_minutes_end_the_session(self):
        self.d._arm_idle()
        self.fire()
        self.assertTrue(self.d.brief_due)
        self.assertEqual(self.d.session.turns, [])
        self.run_pending()
        self.summarize.assert_awaited_once_with(self.d.brain, [{"question": "q"}])

    def test_a_newer_question_restarts_the_wait(self):
        self.d._arm_idle()
        self.d._arm_idle()
        self.fire(0)  # the first timer is stale
        self.assertEqual(self.d.session.turns, [{"question": "q"}])
        self.fire(1)
        self.assertEqual(self.d.session.turns, [])
        self.run_pending()

    def test_not_while_busy_or_quitting(self):
        self.d._arm_idle()
        self.d.busy = True
        self.fire()
        self.assertEqual(len(self.timers), 2)  # waits again
        self.assertTrue(self.d.session.turns)
        self.d.quitting = True
        self.fire()
        self.assertTrue(self.d.session.turns)

    def test_a_failed_summary_is_only_logged(self):
        self.summarize.side_effect = RuntimeError("secret content")
        self.aloop.run_until_complete(self.d._summarize([{"question": "q"}]))
        logged = " ".join(str(c) for c in self.d._summarize.__globals__["log"].call_args_list)
        self.assertIn("RuntimeError", logged)
        self.assertNotIn("secret content", logged)

    def test_no_turns_no_summary(self):
        self.d.session.turns = []
        self.d._end_session()
        self.assertIsNone(self.d.context_future)
        self.assertTrue(self.d.brief_due)


class Wiring(unittest.TestCase):
    """Where the daemon calls it: every saved question feeds the session; /new, quit and the first question."""
    src = (Path(__file__).parents[1] / "flippy/daemon.py").read_text()

    def body(self, name):
        tree = ast.parse(self.src)
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Flippy")
        return ast.unparse(next(n for n in cls.body if getattr(n, "name", None) == name))

    def test_hooks(self):
        self.assertIn("self.session.add(", self.body("_save_history"))
        self.assertIn("self._arm_idle()", self.body("_save_history"))
        for name in ("reset_session", "quit"):
            self.assertIn("self.session.take()", self.body(name), name)
            self.assertIn("self._summarize(turns", self.body(name), name)
        ask = self.body("_ask")
        self.assertIn("context.brief(", ask)
        self.assertLess(ask.index("brain.reset()"), ask.index("context.brief("))


if __name__ == "__main__":
    unittest.main()
