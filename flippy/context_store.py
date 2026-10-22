"""The context folder (#22): ongoing tasks Flippy keeps for itself across its 15-minute session resets.

When a session ends, one small text-only call (flippy/context.py) reads what was asked and decides which task it
belongs to: an existing one, a new one, or none. Each task is a Markdown file in the profile's config folder
(context/, owner-only), text only, never screenshots:

    # Mixing the EP
    apps: Logic Pro, Finder
    updated: 2026-10-22T14:03

    ## Where you are
    Mixing track 3; sidechain works, bus routing still not.

    ## Log
    - 2026-10-22: set up sidechain compression on the kick

Flippy never decides a task is finished. One not picked up for context.compact_days (90 by default) is compacted to
a few lines (title, apps, last date and what was done); picking it up again makes it active.
"""
import os
import re
import secrets
import time
from datetime import datetime

from . import settings
from .profile import current

DIR = os.path.join(current().config_dir, "context")
LOG_LINES = 20      # a task's log keeps its latest lines
SUMMARY_LINES = 3   # a compacted task keeps its last few things done
INDEX_TASKS = 6     # active tasks listed for the summarizer and the read-back
BRIEF_CHARS = 1800  # the read-back stays a few hundred tokens
MAX_TITLE, MAX_LINE, MAX_APPS = 80, 300, 8
_ID = re.compile(r"[a-z0-9-]{1,64}")
_listeners = []


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


def _clean(text, limit=MAX_LINE):
    return " ".join(str(text or "").split())[:limit]


def _stamp(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else time.time()).strftime("%Y-%m-%dT%H:%M")


def _date(ts=None):
    return _stamp(ts)[:10]


def _ts(stamp):
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M").timestamp()
    except (TypeError, ValueError):
        return 0.0


def new_task(title, ts=None):
    return {"id": None, "title": _clean(title, MAX_TITLE) or "Untitled", "apps": [], "updated": _stamp(ts),
            "where": "", "log": [], "compacted": False, "summary": []}


# --- the file format ---
def render(task):
    lines = [f"# {task['title']}", f"apps: {', '.join(task['apps'])}", f"updated: {task['updated']}"]
    if task["compacted"]:
        lines.append("compacted: yes")
        lines += ["", "## What you did"] + [f"- {line}" for line in task["summary"]]
    else:
        if task["where"]:
            lines += ["", "## Where you are", task["where"]]
        if task["log"]:
            lines += ["", "## Log"] + [f"- {day}: {text}" for day, text in task["log"]]
    return "\n".join(lines) + "\n"


def parse(text, task_id=None):
    task, section = new_task(""), None
    task["id"], task["updated"] = task_id, ""
    for line in text.splitlines():
        line = line.rstrip()
        if line.startswith("# ") and section is None and task["title"] == "Untitled":
            task["title"] = _clean(line[2:], MAX_TITLE) or "Untitled"
        elif line.startswith("## "):
            section = line[3:].strip().lower()
        elif section is None and ":" in line:
            key, value = (part.strip() for part in line.split(":", 1))
            if key == "apps":
                task["apps"] = [_clean(a, 60) for a in value.split(",") if a.strip()][:MAX_APPS]
            elif key == "updated":
                task["updated"] = value
            elif key == "compacted":
                task["compacted"] = value.lower() == "yes"
        elif section == "where you are" and line:
            task["where"] = _clean(f"{task['where']} {line}", MAX_LINE * 2)
        elif section == "log" and line.startswith("- "):
            day, _, done = line[2:].partition(": ")
            if done:
                task["log"].append((day[:10], _clean(done)))
        elif section == "what you did" and line.startswith("- "):
            task["summary"].append(_clean(line[2:]))
    return task


# --- the folder ---
def _dir():
    os.makedirs(DIR, mode=0o700, exist_ok=True)
    return DIR


def _path(task_id):
    if not _ID.fullmatch(task_id or ""):
        raise ValueError("bad task id")
    return os.path.join(DIR, task_id + ".md")


def _new_id(title):
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "task"
    return f"{slug}-{secrets.token_hex(2)}"


def tasks():
    """Every task, most recently picked up first."""
    try:
        names = sorted(os.listdir(DIR))
    except FileNotFoundError:
        return []
    found = []
    for name in names:
        task_id = name[:-3]
        if not name.endswith(".md") or not _ID.fullmatch(task_id):
            continue
        try:
            with open(os.path.join(DIR, name), encoding="utf-8") as f:
                found.append(parse(f.read(), task_id))
        except (OSError, UnicodeDecodeError):
            continue
    return sorted(found, key=lambda t: (t["updated"], t["id"]), reverse=True)


def get(task_id):
    try:
        with open(_path(task_id), encoding="utf-8") as f:
            return parse(f.read(), task_id)
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def save(task):
    """Write a task (a new one gets an id). Owner-only, replaced in one step."""
    _dir()
    task["id"] = task["id"] or _new_id(task["title"])
    task["log"] = task["log"][-LOG_LINES:]
    path = _path(task["id"])
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(render(task))
    os.replace(tmp, path)
    _changed()
    return task


def rename(task_id, title):
    task = get(task_id)
    if task and _clean(title, MAX_TITLE):
        task["title"] = _clean(title, MAX_TITLE)
        save(task)
    return task


def delete(task_id):
    try:
        os.remove(_path(task_id))
    except (FileNotFoundError, ValueError):
        return
    _changed()


def clear():
    for task in tasks():
        delete(task["id"])


# --- compacted by time, never by judgment ---
def compact(task):
    """A few lines: title, apps, last date, and the last things done (the "where you are" and older log go)."""
    if task["compacted"]:
        return task
    done = [text for _, text in task["log"][-SUMMARY_LINES:]] or ([task["where"]] if task["where"] else [])
    return {**task, "compacted": True, "summary": done, "where": "", "log": []}


def compact_due(now=None):
    """Compact every task not picked up for context.compact_days. Returns how many."""
    cutoff = (now or time.time()) - settings.get("context", "compact_days") * 86400
    due = [t for t in tasks() if not t["compacted"] and _ts(t["updated"]) < cutoff]
    for task in due:
        save(compact(task))
    return len(due)


def reopen(task):
    """A compacted task picked up again: active, its summary back as the start of its log."""
    if not task["compacted"]:
        return task
    day = task["updated"][:10]
    return {**task, "compacted": False, "log": [(day, line) for line in task["summary"]], "summary": []}


# --- what a session did (the summarizer's decisions, flippy/context.py) ---
def apply(changes, ts=None):
    """changes: [{"id": existing id or None, "title", "apps", "where", "done": [lines]}]. Returns the saved tasks."""
    saved = []
    for change in changes:
        task = get(change.get("id")) if change.get("id") else None
        if task is None:
            if not _clean(change.get("title"), MAX_TITLE):
                continue
            task = new_task(change["title"], ts)
        task = reopen(task)
        apps = [_clean(a, 60) for a in change.get("apps", ()) if _clean(a, 60)]
        task["apps"] = list(dict.fromkeys(task["apps"] + apps))[-MAX_APPS:]
        if _clean(change.get("where")):
            task["where"] = _clean(change["where"], MAX_LINE * 2)
        task["log"] += [(_date(ts), _clean(line)) for line in change.get("done", ()) if _clean(line)]
        task["updated"] = _stamp(ts)
        saved.append(save(task))
    return saved


def index(limit=INDEX_TASKS):
    """The summarizer's view: recent active tasks, then compacted titles, one line each."""
    lines = []
    active = [t for t in tasks() if not t["compacted"]]
    for t in active[:limit]:
        lines.append(f"- id={t['id']} | {t['title']} | apps: {', '.join(t['apps']) or '?'} | "
                     f"last: {t['updated'][:10]} | where: {t['where'] or '?'}")
    for t in [t for t in tasks() if t["compacted"]][:limit * 2]:
        lines.append(f"- id={t['id']} | {t['title']} | apps: {', '.join(t['apps']) or '?'} | "
                     f"last: {t['updated'][:10]} | compacted")
    return "\n".join(lines)


_STOP = set("the a an and or to of in on for with how do does did i my me is it this that what where why can you "
            "your last time again into from at by be was are".split())


def _words(text):
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2 and w not in _STOP}


def brief(app="", question=""):
    """The read-back for a new session's first question: recent active tasks in a line each, the full file of the
    likeliest one (same app, most recent), and a compacted task only when the question clearly matches it."""
    all_tasks = tasks()
    active = [t for t in all_tasks if not t["compacted"]][:INDEX_TASKS]
    app = (app or "").lower()
    likely = next((t for t in active if app and app in (a.lower() for a in t["apps"])), None)
    words = _words(question)
    recalled = [t for t in all_tasks if t["compacted"]
                and len(words & _words(t["title"] + " " + " ".join(t["summary"]))) >= 2][:1]
    if not active and not recalled:
        return ""
    parts = []
    others = [t for t in active if t is not likely]  # (the likeliest one is there in full)
    if others:
        parts.append("Ongoing tasks:\n" + "\n".join(
            f"- {t['title']} ({', '.join(t['apps']) or 'no app'}, {t['updated'][:10]}): {t['where'] or 'no notes'}"
            for t in others))
    for t in ([likely] if likely else []) + recalled:
        parts.append(render(t).strip())
    text = "\n\n".join(parts)
    return text[:BRIEF_CHARS].rstrip()
