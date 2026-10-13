"""Conversation history for the app window's History page: what you asked, what Flippy answered or did.

Text only, in the profile's config folder (history.sqlite3, owner-only): never screenshots, video or drawn marks.
Kept history.days days (30 by default); off when history.keep is off. One row per question or desktop task, so
it doesn't need to guess which uses belong together.

The page reads it through list_entries / delete / clear (flippy/shell/bridge.py); the daemon adds rows.
"""
import json
import os
import sqlite3
import time

from . import settings
from .profile import current

PATH = os.path.join(current().config_dir, "history.sqlite3")
MAX_TEXT = 20_000
_listeners = []
_db = None


def _conn():
    global _db
    if _db is None:
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        fd = os.open(PATH, os.O_CREAT | os.O_RDWR, 0o600)  # created owner-only before sqlite opens it
        os.close(fd)
        _db = sqlite3.connect(PATH, check_same_thread=False, timeout=5)  # waits out another writer (a standalone window)
        _db.execute("PRAGMA journal_mode=WAL")
        _db.execute("""CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, question TEXT NOT NULL,
            answer TEXT NOT NULL DEFAULT '', steps TEXT NOT NULL DEFAULT '[]', app TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL DEFAULT '', outcome TEXT NOT NULL DEFAULT 'completed', detail TEXT NOT NULL DEFAULT '')""")
        _db.execute("CREATE INDEX IF NOT EXISTS entries_ts ON entries (ts)")
        _db.commit()
    return _db


def close():
    global _db
    if _db is not None:
        _db.close()
        _db = None


def on_change(fn):
    _listeners.append(fn)


def off_change(fn):
    if fn in _listeners:
        _listeners.remove(fn)


def _changed():
    for fn in list(_listeners):
        try:
            fn()
        except Exception:
            pass


def _prune(db):
    """Drop entries older than history.days. Committed here, so a read never leaves a write open (or undone)."""
    with db:
        db.execute("DELETE FROM entries WHERE ts < ?", (time.time() - settings.get("history", "days") * 86400,))


def add(kind, question, answer="", steps=(), app="", provider="", outcome="completed", detail="", ts=None):
    """Save one question or task. Returns its id, or None when history is off."""
    if not settings.get("history", "keep") or not (question or "").strip():
        return None
    db = _conn()
    _prune(db)
    with db:  # commits, or rolls back if the insert fails
        cur = db.execute(
            "INSERT INTO entries (ts, kind, question, answer, steps, app, provider, outcome, detail) VALUES (?,?,?,?,?,?,?,?,?)",
            (ts or time.time(), kind, question.strip()[:MAX_TEXT], (answer or "").strip()[:MAX_TEXT],
             json.dumps([s[:500] for s in steps][:50]), (app or "")[:200], (provider or "")[:100], outcome, (detail or "")[:500]))
    _changed()
    return cur.lastrowid


def _row(r):
    return {"id": r[0], "ts": r[1], "kind": r[2], "question": r[3], "answer": r[4], "steps": json.loads(r[5] or "[]"),
            "app": r[6], "provider": r[7], "outcome": r[8], "detail": r[9]}


def list_entries(offset=0, limit=50, query=None):
    """Newest first. offset: how many to skip (paging); query: words that must all appear."""
    db = _conn()
    _prune(db)
    sql, args = "SELECT * FROM entries WHERE 1=1", []
    for word in (query or "").lower().split()[:8]:
        sql += " AND (lower(question) LIKE ? OR lower(answer) LIKE ? OR lower(app) LIKE ?)"
        args += [f"%{word}%"] * 3
    sql += " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?"
    args += [max(1, min(int(limit), 200)), max(0, int(offset))]
    return [_row(r) for r in db.execute(sql, args)]


def count():
    return _conn().execute("SELECT count(*) FROM entries").fetchone()[0]


def delete(entry_id):
    db = _conn()
    with db:
        db.execute("DELETE FROM entries WHERE id = ?", (int(entry_id),))
    _changed()


def clear():
    db = _conn()
    with db:
        db.execute("DELETE FROM entries")
    db.execute("VACUUM")
    _changed()
