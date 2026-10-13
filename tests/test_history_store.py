"""History for the app window (flippy/history_store.py): text only, newest first, kept history.days days."""
import os
import stat
import tempfile
import time
import unittest
from unittest import mock

from flippy import history_store, settings
from flippy.shell import bridge


class HistoryStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        history_store.close()
        for target, value in ((history_store, ("PATH", os.path.join(self.tmp.name, "history.sqlite3"))),
                              (settings, ("PATH", os.path.join(self.tmp.name, "config.toml")))):
            p = mock.patch.object(target, value[0], value[1])
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(history_store.close)
        settings.load()

    def test_add_list_order_search_and_file_mode(self):
        now = time.time()
        history_store.add("ask", "older question", "an answer", ts=now - 3600)
        history_store.add("task", "open Spotify", "playing", app="Spotify", detail="3 inputs", ts=now)
        history_store.add("ask", "added late but asked earliest", ts=now - 7200)
        got = history_store.list_entries()
        self.assertEqual([e["question"] for e in got], ["open Spotify", "older question", "added late but asked earliest"])
        self.assertEqual([e["question"] for e in history_store.list_entries(query="spotify")], ["open Spotify"])
        self.assertEqual([e["question"] for e in history_store.list_entries(offset=1, limit=1)], ["older question"])
        self.assertEqual(stat.S_IMODE(os.stat(history_store.PATH).st_mode), 0o600)

    def test_off_means_nothing_is_saved(self):
        settings.set("history", "keep", False)
        self.assertIsNone(history_store.add("ask", "secret"))
        self.assertEqual(history_store.count(), 0)

    def test_old_entries_go_and_delete_clear_work(self):
        settings.set("history", "days", 1)
        history_store.add("ask", "two days old", ts=time.time() - 2 * 86400)
        keep = history_store.add("ask", "fresh")
        self.assertEqual([e["id"] for e in history_store.list_entries()], [keep])
        history_store.delete(keep)
        history_store.add("ask", "a")
        history_store.clear()
        self.assertEqual(history_store.count(), 0)

    def test_bridge_ops_and_push(self):
        pushed = []
        b = bridge.Bridge(push=lambda e, d: pushed.append(e))
        self.addCleanup(b.close)
        history_store.add("ask", "hello")
        self.assertIn("history", pushed)
        r = b.handle({"id": 1, "op": "history_list", "args": {"query": "hel"}})["result"]
        self.assertEqual((len(r["entries"]), r["keep"], r["days"]), (1, True, 30))
        b.handle({"id": 2, "op": "history_delete", "args": {"id": r["entries"][0]["id"]}})
        self.assertEqual(history_store.count(), 0)
        self.assertFalse(b.handle({"id": 3, "op": "history_list", "args": {"limit": "x"}})["ok"])


if __name__ == "__main__":
    unittest.main()


class DaemonRemembers(unittest.TestCase):
    """flippy/daemon.py _remember: what a finished question or task saves."""
    def remember(self):
        import ast
        from pathlib import Path
        from types import SimpleNamespace
        from flippy.point import segments
        tree = ast.parse((Path(__file__).parents[1] / "flippy/daemon.py").read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Flippy")
        node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_remember")
        self.store = mock.Mock()
        ns = {"segments": segments, "history_store": self.store, "log": mock.Mock(),
              "settings": SimpleNamespace(get=lambda s, k: "claude")}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<remember>", "exec"), ns)
        return ns["_remember"], SimpleNamespace

    def test_answer_steps_without_tags(self):
        remember, NS = self.remember()
        req = NS(history={"question": "how do I turn on Wi-Fi", "app": "Finder", "ts": 5.0})
        remember(None, req, "ask", raw="Open Control Center [POINT:10,10:Control Center] then Wi-Fi [POINT:20,20:Wi-Fi]")
        args = self.store.add.call_args.args
        self.assertEqual(args[:3], ("ask", "how do I turn on Wi-Fi", "Open Control Center then Wi-Fi"))
        self.assertEqual(args[3], ["Open Control Center  → Control Center", "then Wi-Fi  → Wi-Fi"])
        self.assertNotIn("POINT", repr(args))
        self.assertIsNone(req.history)  # saved once
        remember(None, req, "ask", raw="again")
        self.assertEqual(self.store.add.call_count, 1)

    def test_task_and_no_history(self):
        remember, NS = self.remember()
        tools = NS(failure=None, actions=2, app_names={"com.spotify.client": "Spotify"})
        remember(None, NS(history={"question": "play jazz", "app": "Finder", "ts": 1.0}), "task", tools=tools, result="Playing jazz.")
        args = self.store.add.call_args.args
        self.assertEqual((args[2], args[4], args[7]), ("Playing jazz.", "Spotify", "2 inputs"))
        remember(None, NS(identity=3), "task", tools=tools, result="x")  # a request with nothing to save
        self.assertEqual(self.store.add.call_count, 1)


class HistoryTransactions(HistoryStore):
    """Reading prunes old entries durably and leaves no write open (a second writer isn't blocked)."""
    def test_list_commits_its_pruning(self):
        import sqlite3
        settings.set("history", "days", 1)
        history_store.add("ask", "old", ts=time.time() - 3 * 86400)
        settings.set("history", "days", 2)  # add() pruned with days=1 already; make the read do it instead
        history_store.close()
        raw = sqlite3.connect(history_store.PATH)
        with raw:
            raw.execute("INSERT INTO entries (ts, kind, question) VALUES (?, 'ask', 'stale')", (time.time() - 5 * 86400,))
        raw.close()
        history_store.list_entries()
        self.assertFalse(history_store._conn().in_transaction)
        history_store.close()  # nothing pending to undo
        other = sqlite3.connect(history_store.PATH, timeout=0.2)
        self.assertEqual(other.execute("SELECT count(*) FROM entries WHERE question='stale'").fetchone()[0], 0)
        history_store.list_entries()
        with other:  # a second connection can write right after a read
            other.execute("INSERT INTO entries (ts, kind, question) VALUES (?, 'ask', 'from elsewhere')", (time.time(),))
        other.close()
        self.assertIn("from elsewhere", [e["question"] for e in history_store.list_entries()])
