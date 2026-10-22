"""Summing up a session for the context folder (#22, flippy/context_store.py), and reading it back.

The daemon keeps the session's turns (what was asked, in which app, what Flippy said or did) in memory. When the
session ends (15 minutes without a question, /new, or quit), one small text-only call gets those turns and the task
index and says which tasks to update or start; a one-off session writes nothing. The next session's first question
gets a short brief of the ongoing tasks (context_store.brief).
"""
import json
import time

from . import context_store, settings

MAX_TURNS = 30          # a session's latest turns go to the summarizer
TURN_CHARS = 600        # each question and answer, cut
SUMMARY_TIMEOUT_S = 90

PROMPT = """\
you keep a short notebook of the ongoing tasks a user works on with flippy, a screen tutor. you get the turns of
one session (what they asked, in which app, and what flippy answered or did) and the notebook's index.

decide what this session adds:
- a turn that continues a task in the index updates it (use its id). a task can span apps and days.
- real ongoing work that isn't in the index starts a new task (id null) with a short title naming the goal, not
  the app ("mixing the EP", "setting up the home server", "learning pivot tables").
- one session can touch two tasks. one-off questions ("what's this icon", "what time is it in tokyo") aren't tasks:
  leave them out. if nothing in the session is worth remembering, return no tasks.
- never decide a task is finished; just note where they are.

for each task:
- "where": one line, where they are now and what's next or still not working, in the second person.
- "done": up to 3 short lines of what got done this session and how (the setting, menu or steps that did it).
- "apps": the apps involved this session.

reply with only JSON: {"tasks": [{"id": "id from the index or null", "title": "...", "apps": ["..."],
"where": "...", "done": ["..."]}]}
"""


def enabled():
    return bool(settings.get("context", "keep"))


def skipped(app):
    """An app never to remember (context.skip_apps: names, any case)."""
    names = {name.lower() for name in settings.get_list("context", "skip_apps")}
    return bool(app) and app.lower() in names


class Session:
    """This session's turns, in memory only."""
    def __init__(self):
        self.turns = []

    def add(self, kind, question, answer, app, ts=None):
        if not enabled() or skipped(app) or not (question or "").strip():
            return
        self.turns.append({"ts": ts or time.time(), "kind": kind, "app": app or "",
                           "question": " ".join(question.split())[:TURN_CHARS],
                           "answer": " ".join((answer or "").split())[:TURN_CHARS]})
        del self.turns[:-MAX_TURNS]

    def take(self):
        turns, self.turns = self.turns, []
        return turns


def request(turns):
    """The summarizer's message: the index, then the session."""
    lines = ["Notebook index:", context_store.index() or "(empty)", "", "This session:"]
    for t in turns:
        when = time.strftime("%H:%M", time.localtime(t["ts"]))
        verb = "asked flippy to do" if t["kind"] == "task" else "asked"
        lines.append(f"[{when}, {t['app'] or 'unknown app'}] they {verb}: {t['question']}")
        if t["answer"]:
            lines.append(f"  flippy: {t['answer']}")
    return "\n".join(lines)


def parse(text):
    """The tasks out of a reply (tolerates code fences and chatter). Unknown ids become new tasks."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        raw = json.loads(text[start:end + 1])
    except ValueError:
        return []
    known = {t["id"] for t in context_store.tasks()}
    changes = []
    for item in (raw.get("tasks") if isinstance(raw, dict) else None) or []:
        if not isinstance(item, dict):
            continue
        task_id = item.get("id") if item.get("id") in known else None
        if task_id is None and not str(item.get("title") or "").strip():
            continue
        done = item.get("done") if isinstance(item.get("done"), list) else []
        apps = item.get("apps") if isinstance(item.get("apps"), list) else []
        changes.append({"id": task_id, "title": str(item.get("title") or ""), "where": str(item.get("where") or ""),
                        "done": [str(d) for d in done[:3]], "apps": [str(a) for a in apps]})
    return changes


async def summarize(brain, turns):
    """Write what a finished session did to the context folder. Returns the saved tasks."""
    if not turns or not enabled():
        return []
    reply = await brain.write_text(PROMPT, request(turns))
    saved = context_store.apply(parse(reply), ts=turns[-1]["ts"])
    context_store.compact_due()
    return saved


def brief(app, question):
    """The note added to a new session's first question, or ""."""
    if not enabled() or skipped(app):
        return ""
    text = context_store.brief(app, question)
    if not text:
        return ""
    return ("[Flippy's notes from my earlier sessions. Use them only if this question continues one; don't mention "
            "them otherwise.\n" + text + "]\n\n")
