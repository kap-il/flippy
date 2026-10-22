"""Installed Codex, with ChatGPT authentication and ephemeral app-server threads.

Protocol: https://developers.openai.com/codex/app-server (Codex 0.153.4).

Flippy runs Codex in a home of its own (codex_home: CODEX_HOME), the way it runs Claude with setting_sources=[]:
whatever the user has set up for their own Codex (config.toml, AGENTS.md, plugins, apps, MCP servers, skills,
hooks) is never loaded, and Flippy never shares state with a Codex app or CLI that's running at the same time.
That home has its own ChatGPT sign-in (Setup runs `codex login` with it), so Flippy never touches the user's
Codex sign-in either. The checks in _isolate and _new_thread stay as a second line: they now hold by construction.

Usage: _check_usage reads the plan's rate limits before anything is sent and the turn watches them while it runs
(flippy/usage_guard.py). A snapshot is not an atomic promise; see usage_guard.py for what that can't rule out.
"""
import asyncio
import contextlib
import glob
import json
import re
import os
import shutil
import tempfile
from pathlib import Path

from .brain import BrainError, SYSTEM_PROMPT, TIPS_PROMPT, parse_tips
from .usage_guard import GUARDS, PlanLimitReached

ISOLATION_BLOCKED = "Codex could not isolate Flippy from other tools and settings."
SUPPORTED_VERSION = "0.153.4"   # the oldest Codex whose app-server protocol Flippy speaks; newer ones are fine
RUNTIME_UNAVAILABLE = "Codex is signed in, but its isolated runtime could not start on this computer."

# These are real capability switches in the 0.153.4 configuration schema.
# environments=[] additionally removes exec_command, apply_patch and view_image.
# Not code_mode_host: on Codex 0.159 it's what hands Flippy's dynamic tools (look, press, menu...) to the model, and
# with it off /act never saw a tool. code_mode / code_mode_only (the model running code itself) stay off.
DISABLED_FEATURES = (
    "shell_tool", "shell_snapshot", "shell_snapshot_v2", "apps", "connectors",
    "plugins", "remote_plugin", "codex_hooks", "hooks", "plugin_hooks",
    "browser_use", "browser_use_external", "computer_use", "in_app_browser",
    "image_generation", "imagegenext", "js_repl", "code_mode",
    "code_mode_only", "multi_agent", "multi_agent_v2", "collab", "memories",
    "memory_tool", "goals", "request_permissions", "request_permissions_tool",
    "tool_suggest", "tool_search", "remote_control", "skill_search", "view_image",
    "deferred_executor", "token_budget", "send_async_message", "realtime_conversation",
    "standalone_web_search", "remote_models", "unbounded_connection_retries",
)
SAFE_CONFIG = {
    "forced_login_method": "chatgpt", "model_provider": "openai",
    "chatgpt_base_url": "https://chatgpt.com/backend-api/",
    # Not openai_base_url: on Codex 0.159 setting it sends ChatGPT-login requests to the API endpoint, which refuses
    # them (401, missing api.responses.write). _isolate checks the user's config doesn't override it instead.
    "web_search": "disabled", "project_doc_max_bytes": 0,
    "tools.update_plan.enabled": False, "tools.experimental_request_user_input.enabled": False,
    "history.persistence": "none", "analytics.enabled": False,
    "otel.exporter": "none", "otel.log_user_prompt": False,
    "notify": [], "include_apps_instructions": False, "include_environment_context": False,
    "include_collaboration_mode_instructions": False,
    "check_for_update_on_startup": False,
    "memories.generate_memories": False, "memories.use_memories": False,
    **{f"features.{name}": False for name in DISABLED_FEATURES},
    "features.skip_host_skill_discovery": True,
}


class CodexError(BrainError):
    """Only fixed, user-readable text may cross the provider boundary. code: see flippy/errors.py. reason: a fixed
    name for the log (never a value from Codex or the user's config), to tell apart failures that share a code."""
    def __init__(self, safe_message, code="CODEX-FAILED", reason=None):
        self.safe_message = safe_message
        self.code = code
        self.reason = reason
        super().__init__(safe_message)


def _not_isolated(reason):
    return CodexError(ISOLATION_BLOCKED, "CODEX-ISOLATION", reason)


class CodexSignInRequired(CodexError):
    def __init__(self, safe_message):
        super().__init__(safe_message, "CODEX-LOGIN")


# Apps opened from the Dock or Finder get PATH=/usr/bin:/bin:/usr/sbin:/sbin, not the terminal's, so a Codex
# installed with Homebrew or npm isn't on it: Flippy said "Connect Codex" while it was signed in.
CODEX_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.local/bin"),
              os.path.expanduser("~/.npm-global/bin"), os.path.expanduser("~/.volta/bin"), os.path.expanduser("~/.bun/bin"))


def _node_version_dirs():
    """bin dirs of Node versions installed with nvm or fnm, newest first: npm puts a global codex there, and
    neither is on the PATH an app or a desktop shortcut gets."""
    def version(path):
        return tuple(int(n) for n in re.findall(r"\d+", os.path.basename(path.rstrip("/")))[:3])
    found = []
    for pattern, tail in ((os.path.expanduser("~/.nvm/versions/node/*"), "bin"),
                          (os.path.expanduser("~/.local/share/fnm/node-versions/*"), "installation/bin")):
        found += [os.path.join(d, tail) for d in sorted(glob.glob(pattern), key=version, reverse=True)]
    return found


def codex_executable():
    """The codex command: on PATH, or in the usual install places."""
    return shutil.which("codex") or shutil.which("codex", path=os.pathsep.join([*CODEX_DIRS, *_node_version_dirs()]))


def _search_path():
    dirs = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    found = codex_executable()
    extra = ([os.path.dirname(found)] if found else []) + list(CODEX_DIRS[:2])  # an npm-installed Codex needs node
    return os.pathsep.join(dict.fromkeys(extra + dirs))


def codex_home():
    """Flippy's own CODEX_HOME, per profile: its ChatGPT sign-in, and nothing of the user's Codex setup."""
    from .profile import current
    home = os.path.join(current().config_dir, "codex")
    os.makedirs(home, mode=0o700, exist_ok=True)
    return home


def child_environment():
    """Flippy's Codex home, without inheriting API keys, proxies, providers or the user's CODEX_HOME."""
    keys = ("HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL")
    env = {key: os.environ[key] for key in keys if key in os.environ}
    env["PATH"] = _search_path()
    env["CODEX_HOME"] = codex_home()
    return env


def _log_rpc_error(method, error):
    """Which request Codex refused and why (its code and the start of its message), so CODEX-FAILED is traceable.
    Codex's error messages describe the request's shape, not the conversation."""
    code = error.get("code") if isinstance(error, dict) else None
    text = str(error.get("message", "")) if isinstance(error, dict) else str(error)
    print(f"flippy: codex: {method} failed: code {code}: {text.splitlines()[0][:160] if text else ''}", flush=True)


def login_command(executable):
    """The shell command Setup runs in a terminal: Codex's own ChatGPT browser sign-in, into Flippy's Codex home.
    Flippy never sees the tokens."""
    import shlex
    env = child_environment()
    return ("unset OPENAI_API_KEY CODEX_API_KEY; export CODEX_HOME=" + shlex.quote(env["CODEX_HOME"])
            + " PATH=" + shlex.quote(env["PATH"]) + "; "
            + shlex.quote(executable) + " -c 'forced_login_method=\"chatgpt\"' login")


def _config_args(config):
    # JSON strings/booleans/arrays are valid TOML values for these overrides.
    return [arg for key, value in config.items()
            for arg in ("-c", f"{key}={json.dumps(value, ensure_ascii=False)}")]


async def _cli_logged_in():
    """A status-only fallback, distinct from a working app-server connection."""
    executable = codex_executable()
    if not executable:
        return False
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            executable, "login", "status", env=child_environment(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
        # Some other auth modes include key fragments in their status. Never
        # print, retain or forward the output; recognize only the fixed line.
        return process.returncode == 0 and any(line.strip() == b"Logged in using ChatGPT"
                                               for line in (stdout + stderr).splitlines())
    except (OSError, asyncio.TimeoutError):
        return False
    finally:
        if process and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()


class _RPC:
    """Demultiplex stdio so interrupt requests work while tools are outstanding."""
    def __init__(self, process, directory):
        self.process, self.directory = process, directory
        self.cwd = directory.name
        self.pending, self.events = {}, asyncio.Queue()
        self.sequence = 0
        self.closed = False
        self.reader = asyncio.create_task(self._read())

    @classmethod
    async def launch(cls, config):
        executable = codex_executable()
        if not executable:
            raise CodexError("Install Codex and sign in with ChatGPT to use it in Flippy.", "CODEX-MISSING")
        directory = tempfile.TemporaryDirectory(prefix="flippy-codex-")
        config = {**config, "log_dir": directory.name, "sqlite_home": directory.name}
        version = None
        try:
            version = await asyncio.create_subprocess_exec(
                executable, "--version", cwd=directory.name, env=child_environment(),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            output, _ = await asyncio.wait_for(version.communicate(), 5)
            if version.returncode or not _new_enough(output.decode().strip()):
                raise CodexError(f"Update Codex to {SUPPORTED_VERSION} or newer to use it in Flippy.", "CODEX-OLD")
            process = await asyncio.create_subprocess_exec(
                executable, "app-server", "--listen", "stdio://", "--strict-config",
                *_config_args(config), cwd=directory.name, env=child_environment(),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=8 * 1024 * 1024,
            )
        except BaseException:
            if version and version.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    version.kill()
                await version.wait()
            directory.cleanup()
            raise
        return cls(process, directory)

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("Invalid message")
                if "method" in message:
                    await self.events.put(message)
                elif "id" in message:
                    future = self.pending.pop(message["id"], None)
                    if future is not None and not future.done():
                        if "error" in message:
                            _log_rpc_error(getattr(future, "method", "?"), message["error"])
                            future.set_exception(CodexError("Codex could not complete this request.", "CODEX-FAILED"))
                        else:
                            future.set_result(message.get("result", {}))
        except (OSError, ValueError, TypeError, asyncio.LimitOverrunError):
            pass
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(CodexError("Codex disconnected. Try starting a new chat.", "CODEX-DISCONNECTED"))
            self.pending.clear()
            await self.events.put({"method": "flippy/disconnected"})

    async def send(self, message):
        if self.closed:
            raise CodexError("Codex disconnected. Try starting a new chat.", "CODEX-DISCONNECTED")
        try:
            self.process.stdin.write((json.dumps(message, ensure_ascii=True) + "\n").encode())
            await self.process.stdin.drain()
        except (OSError, RuntimeError):
            raise CodexError("Codex disconnected. Try starting a new chat.", "CODEX-DISCONNECTED") from None

    async def request(self, method, params, timeout=10):
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        future.method = method  # named in the log if Codex answers with an error
        self.pending[request_id] = future
        try:
            await self.send({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise CodexError("Codex did not respond. Try starting a new chat.", "CODEX-NO-RESPONSE") from None
        finally:
            self.pending.pop(request_id, None)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), 2)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.process.kill()
                await self.process.wait()
        finally:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
            self.directory.cleanup()


class CodexProvider:
    def __init__(self):
        self.model, self.effort = None, "low"
        self.dirty = False
        self.blocked_reason = None
        self.auth_source = None
        self._rpc, self._thread = None, None
        self._active_task, self._active_turn, self._desktop = None, None, None
        self._tool_request = None
        self._isolated = False
        self._lock = asyncio.Lock()
        self.history, self.partial, self.replay_context = [], "", False

    def configure(self, model, effort):
        if (self.model, self.effort) != (model, effort):
            self.model, self.effort = model, effort
            self.dirty = self._rpc is not None
            self.history.clear()
            self.replay_context = False

    async def _connect(self, config):
        rpc = await _RPC.launch(config)
        try:
            try:
                version = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
            except OSError:
                version = "0"
            await rpc.request("initialize", {
                "clientInfo": {"name": "flippy", "title": "Flippy", "version": version},
                "capabilities": {"experimentalApi": True},
            })
            await rpc.send({"method": "initialized"})
            await self._check_account(rpc)
            return rpc
        except BaseException:
            await rpc.close()
            raise

    async def _check_account(self, rpc):
        account = (await rpc.request("account/read", {"refreshToken": False})).get("account")
        if not isinstance(account, dict) or account.get("type") != "chatgpt":
            raise CodexSignInRequired("Sign in to Codex with ChatGPT to use it in Flippy.")

    async def available(self):
        """Account metadata only, without starting a thread or model request."""
        rpc = None
        try:
            rpc = await self._connect(SAFE_CONFIG)
            self.blocked_reason = None
            self.auth_source = "app-server"
            return True
        except CodexSignInRequired:
            self.blocked_reason = ("Sign in to Codex for Flippy from Setup. Flippy keeps its own Codex sign-in, "
                                   "separate from your Codex app and settings.")
            self.auth_source = None
            return False
        except Exception:
            if await _cli_logged_in():
                # Signed in, but Flippy can't run it here: not "connected", or a Claude user who also has the Codex
                # CLI would be asked to choose a provider that can't answer.
                self.blocked_reason, self.auth_source = RUNTIME_UNAVAILABLE, "cli"
                return False
            self.blocked_reason = "Codex sign-in could not be checked because its isolated runtime is unavailable."
            self.auth_source = None
            return False
        finally:
            if rpc:
                await rpc.close()

    async def start(self):
        if self.blocked_reason == RUNTIME_UNAVAILABLE:
            raise CodexError(RUNTIME_UNAVAILABLE, "CODEX-RUNTIME")
        if self._rpc is None:
            try:
                self._rpc = await self._connect(SAFE_CONFIG)
            except CodexError:
                raise
            except Exception:
                raise CodexError("Codex could not start. Check its installation and ChatGPT sign-in.", "CODEX-START") from None
        self.dirty = False

    async def stop(self):
        active = self._active_task
        if self._desktop:
            self._desktop.stop()
        if active and active is not asyncio.current_task() and not active.done():
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
        if self._rpc:
            await self._rpc.close()
        self._rpc, self._thread, self._isolated = None, None, False
        self.history.clear()
        self.partial, self.replay_context = "", False

    async def reset(self):
        await self.stop()
        await self.start()

    async def _check_usage(self):
        """Before transmitting anything: refuse unless the plan's included usage is available (flippy/usage_guard.py).
        A check can't stop another app (or this request) from crossing the limit afterwards; the mid-request
        account/rateLimits/updated watch in _run stops it then, and STOP_AT keeps requests away from the edge."""
        report = await self._rpc.request("account/rateLimits/read", None)
        GUARDS["codex"].codex_read(report if isinstance(report, dict) else {})
        GUARDS["codex"].check()

    async def _isolate(self):
        if self._isolated:
            return
        effective = (await self._rpc.request("config/read", {"includeLayers": False})).get("config", {})
        if effective.get("model_provider") != "openai":
            raise _not_isolated("model_provider")
        if effective.get("openai_base_url") not in (None, "", "https://api.openai.com/v1"):
            raise _not_isolated("openai_base_url")  # the user's config points requests elsewhere: don't follow it
        servers = effective.get("mcp_servers", {})
        if not isinstance(servers, dict):
            raise _not_isolated("mcp_servers")
        # Empty TOML maps merge with inherited maps, rather than clearing them.
        # Only IDs survive this read; never log or retain credential-bearing config.
        names = tuple(servers)
        del effective, servers
        if names:
            overrides = {f'mcp_servers.{_toml_key(name)}.enabled': False for name in names}
            await self._rpc.close()
            self._rpc = await self._connect({**SAFE_CONFIG, **overrides})
            effective = (await self._rpc.request("config/read", {"includeLayers": False})).get("config", {})
            if any(server.get("enabled") is not False for server in effective.get("mcp_servers", {}).values()):
                raise _not_isolated("mcp_servers_enabled")
            del effective
        self._isolated = True

    async def _default_model(self):
        """Codex's own default model for this account. Not leaving it to Codex: it would take the model named in
        the user's ~/.codex/config.toml, and one left there from an older Codex (gpt-5.1-codex-max) is refused
        for ChatGPT accounts, so every request failed. None if Codex can't say (then it does pick)."""
        try:
            models = (await self._rpc.request("model/list", {})).get("data", [])
        except (CodexError, OSError, asyncio.TimeoutError):
            return None
        return next((m.get("model") or m.get("id") for m in models if m.get("isDefault")), None)

    async def _new_thread(self, instructions, desktop):
        tools = []
        if desktop:
            tools = [{"type": "function", "name": name, "description": spec["description"],
                      "inputSchema": spec["schema"]} for name, spec in desktop.catalog().items()]
        result = await self._rpc.request("thread/start", {
            "model": self.model or await self._default_model(), "modelProvider": "openai", "allowProviderModelFallback": False,
            "baseInstructions": instructions, "developerInstructions": "", "ephemeral": True,
            "dynamicTools": tools, "environments": [], "runtimeWorkspaceRoots": [],
            "selectedCapabilityRoots": [], "cwd": self._rpc.cwd,
            "approvalPolicy": "never", "sandbox": "read-only",
        })
        for reason, leaked in (("thread_provider", result.get("modelProvider") != "openai"),
                               ("thread_not_ephemeral", not result.get("thread", {}).get("ephemeral")),
                               ("instruction_sources", result.get("instructionSources")),  # e.g. an AGENTS.md
                               ("workspace_roots", result.get("runtimeWorkspaceRoots"))):
            if leaked:
                raise _not_isolated(reason)
        thread_id = result["thread"]["id"]
        cursor = None
        while True:
            inventory = await self._rpc.request("mcpServerStatus/list", {
                "threadId": thread_id, "detail": "toolsAndAuthOnly", "cursor": cursor,
            })
            if any(server.get("tools") or server.get("resources") or server.get("resourceTemplates")
                   for server in inventory.get("data", [])):
                raise _not_isolated("mcp_tools")
            cursor = inventory.get("nextCursor")
            if not cursor:
                break
        return thread_id

    async def _interrupt(self):
        if not self._rpc or not self._active_turn:
            return
        thread_id, turn_id = self._active_turn
        if self._tool_request is not None:
            await self._rpc.send({"id": self._tool_request,
                                  "result": {"contentItems": [{"type": "inputText", "text": "Task stopped."}],
                                             "success": False}})
            self._tool_request = None
        await self._rpc.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=2)
        async with asyncio.timeout(2):
            while True:
                event = await self._rpc.events.get()
                if "id" in event:
                    await self._rpc.send({"id": event["id"],
                                          "error": {"code": -32601, "message": "Task stopped."}})
                params = event.get("params", {})
                if (event.get("method") == "turn/completed" and params.get("threadId") == thread_id
                        and params.get("turn", {}).get("id") == turn_id):
                    return
                if event.get("method") == "flippy/disconnected":
                    return

    async def _tool_call(self, event, desktop, thread_id, turn_id):
        params = event.get("params", {})
        name = params.get("tool")
        if (not desktop or params.get("threadId") != thread_id or params.get("turnId") != turn_id
                or params.get("namespace") is not None or name not in desktop.catalog()
                or not isinstance(params.get("arguments"), dict)):
            await self._rpc.send({"id": event["id"],
                                  "error": {"code": -32601, "message": "Tool unavailable in Flippy."}})
            raise _not_isolated("unknown_tool_call")
        self._tool_request = event["id"]
        result = await desktop.invoke(name, params["arguments"])
        content = []
        for item in result.get("content", []):
            if item.get("type") == "text":
                content.append({"type": "inputText", "text": item["text"]})
            elif item.get("type") == "image" and item.get("mimeType") == "image/jpeg":
                content.append({"type": "inputImage", "imageUrl": "data:image/jpeg;base64," + item["data"]})
            else:
                raise CodexError("Flippy could not return the desktop result.", "ACT-FAILED")
        await self._rpc.send({"id": event["id"],
                              "result": {"contentItems": content, "success": not result.get("is_error", False)}})
        self._tool_request = None
        if desktop.cancel.is_set():
            raise CodexError("Desktop task stopped. Check the screen before continuing.", "ACT-STOPPED")

    def _remember(self, question, reply):
        self.history.append((question[:6000], reply[:12000]))
        while len(self.history) > 12 or sum(len(q) + len(a) for q, a in self.history) > 24000:
            self.history.pop(0)

    async def _run(self, instructions, content, on_text=None, desktop=None, conversation=False, question=""):
        GUARDS["codex"].check()  # already known to be at the limit: don't even connect
        async with self._lock:
            if self.dirty:
                await self.reset()
            await self.start()
            self._active_task, self._desktop = asyncio.current_task(), desktop
            try:
                await self._check_account(self._rpc)
                await self._check_usage()  # before starting a thread or transmitting user content
                await self._isolate()
                thread_id = self._thread if conversation and self._thread else await self._new_thread(instructions, desktop)
                if conversation:
                    self._thread = thread_id
                    if self.replay_context and self.history:
                        context = "\n".join(f"User: {q}\nFlippy: {a}" for q, a in self.history)
                        content.insert(0, {"type": "text", "text": "Earlier conversation, retained in memory after interruption:\n" + context})
                    self.replay_context, self.partial = False, ""
                result = await self._rpc.request("turn/start", {
                    "threadId": thread_id, "input": content, "effort": self.effort,
                    "environments": [], "runtimeWorkspaceRoots": [], "serviceTierForTurn": "default",
                })
                turn_id = result["turn"]["id"]
                self._active_turn = thread_id, turn_id
                messages, final_items, tool_calls = {}, [], 0
                while True:
                    event = await self._rpc.events.get()
                    method, params = event.get("method"), event.get("params", {})
                    if "id" in event:
                        if method != "item/tool/call":
                            await self._rpc.send({"id": event["id"], "error": {
                                "code": -32601, "message": "Capability unavailable in Flippy."}})
                            raise _not_isolated("server_request:" + re.sub(r"[^\w/.-]", "", str(method))[:60])
                        tool_calls += 1
                        if desktop and tool_calls > desktop.max_turns:
                            await self._rpc.send({"id": event["id"], "result": {
                                "contentItems": [{"type": "inputText", "text": "Task tool limit reached."}],
                                "success": False}})
                            raise CodexError("Desktop task reached its tool limit. Check the screen before continuing.", "ACT-LIMIT")
                        await self._tool_call(event, desktop, thread_id, turn_id)
                        continue
                    if method == "flippy/disconnected":
                        raise CodexError("Codex disconnected. Try starting a new chat.", "CODEX-DISCONNECTED")
                    if method == "account/rateLimits/updated":  # mid-request: stop before credits are touched
                        if GUARDS["codex"].codex(params.get("rateLimits") or {}):
                            raise PlanLimitReached(GUARDS["codex"].message())
                        continue
                    if params.get("threadId") != thread_id or params.get("turnId", turn_id) != turn_id:
                        continue
                    if method == "item/agentMessage/delta":
                        item_id = params["itemId"]
                        messages[item_id] = messages.get(item_id, "") + params["delta"]
                        if conversation:
                            self.partial = (self.partial + params["delta"])[-12000:]
                        if on_text:
                            on_text(params["delta"])
                    elif method == "item/completed" and params.get("item", {}).get("type") == "agentMessage":
                        item = params["item"]
                        messages[item["id"]] = item["text"]
                        if item.get("phase") == "final_answer":
                            final_items.append(item["id"])
                        elif desktop is not None and getattr(desktop, "on_thought", None) and item.get("text"):
                            desktop.on_thought(item["text"])  # what Codex says between steps of a task
                    elif method == "turn/completed" and params.get("turn", {}).get("id") == turn_id:
                        if params["turn"].get("status") != "completed":
                            raise CodexError("Codex did not finish this request. Try starting a new chat.", "CODEX-INCOMPLETE")
                        text = messages.get(final_items[-1], "") if final_items else next(reversed(messages.values()), "")
                        if not text.strip():
                            raise CodexError("Codex finished without a reply. Try starting a new chat.", "CODEX-INCOMPLETE")
                        if conversation:
                            self._remember(question, text.strip())
                        return text.strip()
            except BaseException as error:
                if conversation and isinstance(error, asyncio.CancelledError):
                    self._remember(question, self.partial + " [Reply interrupted by user.]")
                    self.replay_context = True
                if desktop:
                    desktop.stop()
                with contextlib.suppress(CodexError, OSError, asyncio.TimeoutError):
                    await self._interrupt()
                await self._rpc.close()
                self._rpc, self._thread, self._isolated = None, None, False
                if isinstance(error, (CodexError, PlanLimitReached, asyncio.CancelledError)):
                    raise
                print(f"flippy: codex: request failed: {type(error).__name__}", flush=True)  # the type, never content
                raise CodexError("Codex could not complete this request. Try starting a new chat.", "CODEX-FAILED") from None
            finally:
                self._active_task, self._active_turn, self._desktop, self._tool_request = None, None, None, None

    async def ask(self, question, b64_jpeg, img_size, on_text=None, extra_images=()):
        content = []
        for label, image in extra_images:
            content.extend([{"type": "text", "text": label},
                            {"type": "image", "url": "data:image/jpeg;base64," + image}])
        w, h = img_size
        content.extend([{"type": "image", "url": "data:image/jpeg;base64," + b64_jpeg},
                        {"type": "text", "text": f"(screenshot is {w}x{h} pixels)\n\n{question}"}])
        return await self._run(SYSTEM_PROMPT, content, on_text, conversation=True, question=question)

    async def act(self, question, desktop):
        from .actions import PROMPT
        # The task's own instructions (the background tools' prompt), like Brain.act; the old coordinate-tool prompt
        # told Codex to take a "screenshot" with a tool it doesn't have, so it answered without acting.
        return await self._run(getattr(desktop, "prompt", PROMPT), [{"type": "text", "text": question}],
                               desktop=desktop)

    async def write_tips(self, app_name, goal, n, skip):
        question = f"App: {app_name}\nWhat they want to do: {goal}\n"
        if skip:
            question += "They already have these (don't repeat them):\n" + "\n".join(skip[-60:]) + "\n"
        question += f"Write {n} tips."
        text = await self._run(TIPS_PROMPT, [{"type": "text", "text": question}])
        return parse_tips(text)

    async def write_text(self, system, text):
        return await self._run(system, [{"type": "text", "text": text}])


def _new_enough(version_line):
    """"codex-cli 0.159.2" -> True when it's SUPPORTED_VERSION or newer."""
    def parts(v):
        return tuple(int(x) for x in v.split("-")[0].split("."))
    try:
        return parts(version_line.removeprefix("codex-cli ").strip()) >= parts(SUPPORTED_VERSION)
    except ValueError:
        return False


def _toml_key(name):
    """One segment of a -c key path. Bare when TOML allows it: newer Codex (0.159) reads a quoted segment
    literally, so `mcp_servers."node_repl".enabled=false` made a new, transport-less server named "node_repl" (with
    the quotes) and --strict-config refused to start. Quoted only when a bare key can't express the name; the
    enabled-is-false check after relaunch still refuses if a server stays on."""
    return name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else json.dumps(name, ensure_ascii=False)
