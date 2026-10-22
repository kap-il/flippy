"""Claude via the Agent SDK on the Pro/Max subscription. Tutor answers and opt-in desktop tasks."""
import asyncio
import os
from dataclasses import replace

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, RateLimitEvent, ResultMessage, StreamEvent,
                              TextBlock, query)

from .usage_guard import GUARDS, PlanLimitReached

# The pointing instructions are adapted from Clicky (MIT: see THIRD_PARTY_NOTICES.md).
SYSTEM_PROMPT = """\
you're a friendly tutor that can see the user's screen. they typed a question; your reply is shown in a tiny box about 1.5 inches wide, off to the side of what they're doing.

rules:
- be brief: usually one sentence, at most two, under 30 words. say the one thing they need (which button, what it does, the next step), not background, caveats or alternatives.
- no filler: don't restate the question, don't open with "great question" or "sure", don't end with "let me know if...". they'll ask a follow-up if they want more.
- go deeper only when they ask for it ("explain", "why", "more", "in detail"), and even then keep it to a few short sentences.
- plain text, no markdown or lists.
- if the question relates to the screen, reference specific things you see. if the screenshot isn't relevant, just answer.
- small icons are easy to misread. if you can't tell what an icon is, say what it looks like and that you're not sure, rather than guessing a name.

pointing:
you have a pointer that can highlight things on screen. use it when the user is looking for a button, menu, setting, or area, or when you walk them through several things on screen. don't point for general-knowledge questions.
to point, put [POINT:x,y:label] right after the words that mention the thing, where x,y are integer pixel coordinates in the screenshot (origin top-left, x right, y down, using the dimensions given) and label is 1-3 words.
the pointer glides to each tag as your reply is read out, and the panel moves with it showing only the text since the previous tag. so when you walk through several things, write one short line per thing (under 12 words), each ending with its tag, in a sensible order (at most 6). one thing = one tag.
if pointing wouldn't help, end with [POINT:none].

learning:
when they're learning or working on something on screen (code, a document, a lesson, a form, a tool they're figuring out) and will want to keep seeing it while they read your reply, start your reply with [LEARN:x1,y1,x2,y2]: the box around what they need to keep seeing, including everything you'll point at, in the same pixel coordinates. flippy then puts your reply where it doesn't cover that. leave it out for a quick "where is it" or general-knowledge question.

tutorials:
only when the message ends with "(tutorial: ...)": mark each step they have to do themselves by adding :click after the label, e.g. [POINT:412,88:Track menu:click]. flippy waits until they click it before going on. when it says "(not a tutorial ...)", never use :click.
if clicking will change the screen (opens a menu, dialog, panel or another view), stop your reply right after that step: you can't see what comes next yet. once they click it you'll get a fresh screenshot and "continue", and you go on from there with the next step(s).

doing things:
only when the message ends with "(you can act ...)", and before the tutorial rules: if they're asking you to do something on their computer for them (open or switch to an app, play, pause or skip music, type or write something into an app, make a note, a draft or a new document, change a setting, click or press something), rather than asking how, where or what, reply with exactly [ACT] and nothing else. flippy then does it with its own tools and asks them before it acts. "open spotify and play some jazz", "write a haiku in this note", "turn on dark mode for me" are [ACT]. "how do i turn on dark mode", "where's the export button", "what does this do" are answered as usual. when it's unclear, answer as usual.
"""

# Model and effort come from settings (flippy-ask settings); env vars override for testing.
ENV_MODEL = os.environ.get("FLIPPY_MODEL")
ENV_EFFORT = os.environ.get("FLIPPY_EFFORT")


TIPS_PROMPT = """\
you write short tips for someone learning an app. each tip is one or two plain sentences (no markdown),
concrete and actionable: name the menu, panel, button or shortcut. skip the obvious. order them from
beginner to advanced and give each a level: 1 (first hour), 2 (first week) or 3 (power user).
reply with only a JSON array: [{"text": "...", "level": 1}, ...]
"""


class BrainError(Exception):
    """kind: Claude's reply error (claude_agent_sdk AssistantMessageError) or "incomplete"; see flippy/errors.py."""
    def __init__(self, message="", kind=None):
        super().__init__(message)
        self.kind = kind


# Claude Code saves every session to ~/.claude/projects/, screenshots included. Flippy's sessions live only in
# memory: follow-ups still work, nothing is written to disk, and they stay out of your Claude Code history.
NO_TRANSCRIPTS = {"no-session-persistence": None}


class Brain:
    def __init__(self):
        # Never bill the API: the SDK picks up ANTHROPIC_API_KEY if it's set.
        os.environ.pop("ANTHROPIC_API_KEY", None)
        self.options = ClaudeAgentOptions(
            system_prompt=SYSTEM_PROMPT,
            tools=[],                 # no built-in tools at all
            allowed_tools=[],
            mcp_servers={},
            strict_mcp_config=True,   # ignore any user/project MCP config
            setting_sources=[],       # don't load CLAUDE.md, hooks, user settings
            max_turns=1,
            include_partial_messages=True,  # text deltas, so the pointer can move as the reply arrives
            model=ENV_MODEL,
            effort=ENV_EFFORT or "low",
            cwd=os.path.expanduser("~"),
            extra_args=dict(NO_TRANSCRIPTS),
        )
        self.client: ClaudeSDKClient | None = None
        self.dirty = False  # options changed; next question starts a fresh session
        self._lock = asyncio.Lock()
        self.history = []
        self.replay_context = False
        self.partial = ""

    def configure(self, model: str | None, effort: str):
        """None model = the account's default. Applies on the next (fresh) session."""
        model, effort = ENV_MODEL or model, ENV_EFFORT or effort
        if (self.options.model, self.options.effort) != (model, effort):
            self.options.model, self.options.effort = model, effort
            self.dirty = self.client is not None
            self.history.clear()
            self.replay_context = False

    async def start(self):
        async with self._lock:
            if self.client is None:
                await self._start()

    async def _start(self):
        client = ClaudeSDKClient(options=self.options)
        try:
            await client.connect()
        except BaseException:
            await client.disconnect()
            raise
        self.client, self.dirty = client, False

    async def stop(self):
        async with self._lock:
            await self._stop()
            self.history.clear()
            self.replay_context = False

    async def _stop(self):
        if self.client:
            client, self.client = self.client, None
            await client.disconnect()

    async def reset(self):
        async with self._lock:
            await self._stop()
            self.history.clear()
            self.replay_context = False
            await self._start()

    @staticmethod
    def _usage(msg):
        """A rate-limit report from Claude: stop this request if it would run on extra usage (flippy/usage_guard.py)."""
        if GUARDS["claude"].claude(msg.rate_limit_info):
            raise PlanLimitReached(GUARDS["claude"].message())

    async def act(self, question, desktop):
        """An isolated tool session using the tutor's existing model/auth settings.

        No API client or key is introduced. Tutor and tip sessions remain tool-free.
        The local handlers enforce approval even though MCP tools are allowed here.
        """
        from .actions import PROMPT, TOOL_CATALOG
        names = list(desktop.catalog()) if hasattr(desktop, "catalog") else list(TOOL_CATALOG)
        # Their own setting (act.effort, low by default): a task waits on Claude at every step, and medium took about
        # twice as long per step as low (3 s, not 1.6 s, in a Calculator task). FLIPPY_EFFORT still overrides.
        from . import settings
        effort = ENV_EFFORT or settings.get("act", "effort")
        opts = replace(self.options, system_prompt=getattr(desktop, "prompt", PROMPT), effort=effort,
                       max_turns=getattr(desktop, "max_turns", 16),
                       include_partial_messages=False, mcp_servers={"desktop": desktop.server()},
                       allowed_tools=[f"mcp__desktop__{name}" for name in names])
        GUARDS["claude"].check()
        async with ClaudeSDKClient(options=opts) as client:
            await client.query(question)
            text = ""
            completed = False
            async for msg in client.receive_response():
                if isinstance(msg, RateLimitEvent):
                    self._usage(msg)
                elif isinstance(msg, AssistantMessage):
                    if getattr(msg, "error", None):
                        raise BrainError(str(msg.error), kind=str(msg.error))
                    # Only the last assistant message is the task's final answer.
                    text = "".join(b.text for b in msg.content if isinstance(b, TextBlock))
                    if text.strip() and getattr(desktop, "on_thought", None):
                        desktop.on_thought(text)
                elif isinstance(msg, ResultMessage):
                    if msg.is_error or msg.subtype != "success":
                        raise BrainError(msg.result or "Desktop task did not finish.", kind="incomplete")
                    text = msg.result or text
                    completed = True
            if desktop.cancel.is_set():
                return f"Task stopped: {desktop.failure or 'canceled'}. Check the screen before continuing."
            if not completed:
                raise BrainError("Desktop task ended without a completed response.", kind="incomplete")
            return text.strip() or "Task finished without a final answer; check the screen."

    async def write_tips(self, app_name: str, goal: str, n: int, skip: list[str]) -> list[dict]:
        """One text-only call, outside the conversation: n tips for app_name. skip: tips they already have."""
        opts = replace(self.options, system_prompt=TIPS_PROMPT, effort="low", include_partial_messages=False,
                       extra_args=dict(NO_TRANSCRIPTS))
        ask = f"App: {app_name}\n"
        if goal:
            ask += f"What they want to do: {goal}\n"
        if skip:
            ask += "They already have these (don't repeat them):\n" + "\n".join(f"- {t}" for t in skip[-60:]) + "\n"
        ask += f"Write {n} tips."
        GUARDS["claude"].check()
        parts, completed = [], False
        async for msg in query(prompt=ask, options=opts):
            if isinstance(msg, RateLimitEvent):
                self._usage(msg)
            elif isinstance(msg, AssistantMessage):
                if getattr(msg, "error", None):
                    raise BrainError(str(msg.error), kind=str(msg.error))
                parts.extend(b.text for b in msg.content if isinstance(b, TextBlock))
            elif isinstance(msg, ResultMessage):
                if msg.is_error or msg.subtype != "success":
                    raise BrainError(msg.result or msg.subtype or "unknown error")
                completed = True
        if not completed:
            raise BrainError("Tip generation ended without a completed response.")
        return parse_tips("".join(parts))

    async def ask(self, question: str, b64_jpeg: str, img_size: tuple[int, int],
                  on_text=None, extra_images=()) -> str:
        """One serialized tutor/video turn; cancellation discards its transport."""
        w, h = img_size
        content = []
        for label, jpeg in extra_images:
            content.extend([{"type": "text", "text": label},
                            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": jpeg}}])
        content.extend([
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64_jpeg}},
            {"type": "text", "text": f"(current screenshot is {w}x{h} pixels)\n\n{question}"},
        ])
        async with self._lock:
            if self.dirty:
                await self._stop()
            if self.client is None:
                await self._start()
            if self.replay_context and self.history:
                context = "\n".join(f"User: {q}\nFlippy: {a}" for q, a in self.history)
                content.insert(0, {"type": "text", "text": "Earlier conversation, retained in memory after interruption:\n" + context})
                self.replay_context = False
            self.partial = ""
            try:
                reply = await self._reply(content, on_text)
                self._remember(question, reply)
                return reply
            except BaseException as error:
                if isinstance(error, asyncio.CancelledError):
                    self._remember(question, self.partial + " [Reply interrupted by user.]")
                    self.replay_context = True
                # Reset cannot race this turn. Do not leave partial context alive.
                cleanup = asyncio.create_task(self._stop())
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
                raise

    def _remember(self, question, reply):
        self.history.append((question[:6000], reply[:12000]))
        while len(self.history) > 12 or sum(len(q) + len(a) for q, a in self.history) > 24000:
            self.history.pop(0)

    async def _reply(self, content, on_text):
        GUARDS["claude"].check()

        async def messages():
            yield {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None}

        await self.client.query(messages())
        parts = []
        completed = False
        async for msg in self.client.receive_response():
            if isinstance(msg, RateLimitEvent):
                self._usage(msg)
            elif isinstance(msg, StreamEvent):
                ev = msg.event
                if ev.get("type") == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
                    delta = ev["delta"]["text"]
                    self.partial = (self.partial + delta)[-12000:]
                    if on_text:
                        on_text(delta)
            elif isinstance(msg, AssistantMessage):
                if getattr(msg, "error", None):
                    raise BrainError(str(msg.error), kind=str(msg.error))
                parts.extend(b.text for b in msg.content if isinstance(b, TextBlock))
            elif isinstance(msg, ResultMessage):
                if msg.is_error or msg.subtype != "success":
                    raise BrainError("The response did not complete.", kind="incomplete")
                completed = True
        if not completed:
            raise BrainError("The response ended before completion.", kind="incomplete")
        text = "".join(parts).strip()
        if not text:
            raise BrainError("empty reply", kind="incomplete")
        return text


def parse_tips(text: str) -> list[dict]:
    """The JSON array out of a reply (tolerates code fences and chatter around it)."""
    import json
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        raise BrainError("no tips in the reply")
    try:
        raw = json.loads(text[start:end + 1])
    except ValueError as e:
        raise BrainError(f"couldn't read the tips: {e}") from e
    tips = []
    for item in raw:
        if isinstance(item, dict) and str(item.get("text", "")).strip():
            level = item.get("level", 2)
            tips.append({"text": str(item["text"]).strip(), "level": level if level in (1, 2, 3) else 2})
    if not tips:
        raise BrainError("no tips in the reply")
    return tips
