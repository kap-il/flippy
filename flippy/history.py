"""The question box's history: Up recalls earlier prompts, Down comes back, like a shell.

In memory only, for as long as Flippy runs: prompts never go to disk. Shared by the macOS and COSMIC boxes;
only what's submitted from the box is kept (not approval cards, setup fields or flippy-ask).
"""

SIZE = 50
# the line under the box's field, both boxes: as usual, and for a follow-up in the middle of a walkthrough (#26)
BOX_HINT = "Enter to ask or get it done · ↑ earlier · Esc to close · /new · /settings"
FOLLOW_UP_HINT = "Enter to ask about this step · Esc back to the walkthrough"
SECOND_FOLLOW_UP_HINT = "Enter to ask about this step · Esc to close"  # (a side answer or a follow-up stays up)
# the same, for the macOS box, which is about 1.5 inches wide (#37)
SHORT_HINTS = {BOX_HINT: "↵ ask · ↑ earlier · esc close", FOLLOW_UP_HINT: "↵ ask about this step · esc back",
               SECOND_FOLLOW_UP_HINT: "↵ ask about this step · esc close"}


class PromptHistory:
    def __init__(self, size=SIZE):
        self.size = size
        self.items = []
        self.pos = None     # index being shown, or None at the newest end (the draft)
        self.draft = ""     # what was typed before pressing Up, back after Down past the newest

    def add(self, text):
        text = text.strip()
        if text and (not self.items or self.items[-1] != text):  # the same prompt twice in a row is kept once
            self.items.append(text)
            del self.items[:-self.size]
        self.reset()

    def reset(self):
        """Back to the newest end: the box opened, closed or sent."""
        self.pos, self.draft = None, ""

    def older(self, current):
        """The prompt before the one shown (current: the box's text now), or None if there isn't one."""
        if not self.items or self.pos == 0:
            return None
        if self.pos is None:
            self.draft, self.pos = current, len(self.items)
        self.pos -= 1
        return self.items[self.pos]

    def newer(self, current):
        """The prompt after the one shown, the draft past the newest, or None when already at the draft."""
        if self.pos is None:
            return None
        self.pos += 1
        if self.pos >= len(self.items):
            draft = self.draft
            self.reset()
            return draft
        return self.items[self.pos]


PROMPTS = PromptHistory()
