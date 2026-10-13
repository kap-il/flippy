"""One lifetime for capture, inference, approvals and the resulting playback."""
import threading
from dataclasses import dataclass, field


@dataclass(eq=False)
class Request:
    identity: int
    mode: str
    canceled: threading.Event = field(default_factory=threading.Event)
    future: object = None
    pending: bool = True
    drained: threading.Event = field(default_factory=threading.Event)
    timers: set = field(default_factory=set)
    history: object = None  # what to save when it ends (flippy/history_store.py): the question, the app

    def owns(self, current):
        return current is self and not self.canceled.is_set()

    def cancel(self, remove_timer):
        """Idempotent, UI-thread teardown; native workers share the cancellation event."""
        if self.canceled.is_set():
            return False
        self.canceled.set()
        for timer in tuple(self.timers):
            try:
                remove_timer(timer)
            except Exception:
                pass  # continue canceling the transport even if a native timer already expired
        self.timers.clear()
        if self.future is not None and self.pending:
            self.future.cancel()
        return True
