"""Choose one native subscription connection, without sharing provider history."""
import asyncio
import json
import os
import shutil

from . import settings
from .brain import Brain as ClaudeBrain, BrainError


class ProviderChoiceRequired(BrainError):
    """kind: claude-login, codex-login, none (nothing connected) or pick (both connected on Automatic)."""


CONNECTION_STATUS = {"claude": None, "codex": None}


def setup_ready():
    choice = settings.get("provider", "mode")
    if choice == "auto":
        return sum(value is True for value in CONNECTION_STATUS.values()) == 1
    return CONNECTION_STATUS[choice] is True


async def claude_available():
    """Signed in to Claude Code with a claude.ai subscription? A missing or broken install is "not connected"."""
    try:
        from claude_agent_sdk import __file__ as sdk_file
    except ImportError:
        return False
    bundled = os.path.join(os.path.dirname(sdk_file), "_bundled", "claude")
    cli = bundled if os.path.isfile(bundled) else shutil.which("claude")
    if not cli:
        return False
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    try:
        process = await asyncio.create_subprocess_exec(cli, "auth", "status", env=env,
                                                     stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.DEVNULL)
    except OSError:  # not executable, wrong architecture, vanished...
        return False
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 10)
        status = json.loads(output)
        return process.returncode == 0 and status.get("loggedIn") is True and status.get("authMethod") == "claude.ai"
    except (ValueError, asyncio.TimeoutError):
        return False
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


class Brain:
    def __init__(self):
        from .codex_provider import CodexProvider
        self.providers = {"claude": ClaudeBrain(), "codex": CodexProvider()}
        self.selected = None
        self.dirty = False
        self._lock = asyncio.Lock()
        self._revision = 0

    def configure(self, model=None, effort="low"):
        self.providers["claude"].configure(model, effort)
        codex_model = settings.get("codex", "model")
        self.providers["codex"].configure(None if codex_model == "default" else codex_model,
                                          settings.get("codex", "effort"))
        self.dirty = True
        self._revision += 1

    async def _select(self):
        mode = settings.get("provider", "mode")
        if mode != "auto":
            CONNECTION_STATUS[mode] = (await claude_available() if mode == "claude"
                                        else await self.providers["codex"].available())
            if CONNECTION_STATUS[mode] is not True:
                raise ProviderChoiceRequired("Connect " + ("Claude Code" if mode == "claude" else "Codex / ChatGPT")
                                             + " with your subscription in Setup. API-key connections are refused.",
                                             kind=mode + "-login")
            return mode
        status = await self.connections()
        claude, codex = status["claude"], status["codex"]
        if claude != codex:
            return "claude" if claude else "codex"
        raise ProviderChoiceRequired("Both subscriptions are connected. Choose Claude or ChatGPT in Settings → Models."
                                     if claude else "Connect Claude Code or Codex with your subscription, then choose it in Settings → Models.",
                                     kind="pick" if claude else "none")

    async def connections(self):
        claude, codex = await asyncio.gather(claude_available(), self.providers["codex"].available())
        CONNECTION_STATUS.update(claude=claude, codex=codex)
        return dict(CONNECTION_STATUS)

    async def start(self):
        async with self._lock:
            while True:
                revision = self._revision
                if not self.selected or self.dirty:
                    if self.selected:
                        await self.providers[self.selected].stop()
                    self.selected = None
                    candidate = await self._select()
                    if revision != self._revision:
                        continue
                    self.selected = candidate
                    self.dirty = False
                await self.providers[self.selected].start()
                if revision == self._revision:
                    return
                self.dirty = True

    async def stop(self):
        async with self._lock:
            for provider in self.providers.values():
                await provider.stop()
            self.selected = None

    async def reset(self):
        await self.stop()
        await self.start()

    async def ask(self, *args, **kwargs):
        await self.start()
        return await self.providers[self.selected].ask(*args, **kwargs)

    async def act(self, *args, **kwargs):
        await self.start()
        return await self.providers[self.selected].act(*args, **kwargs)

    async def write_tips(self, *args, **kwargs):
        await self.start()
        return await self.providers[self.selected].write_tips(*args, **kwargs)

    async def write_text(self, *args, **kwargs):
        await self.start()
        return await self.providers[self.selected].write_text(*args, **kwargs)
