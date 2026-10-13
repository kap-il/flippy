"""Flippy daemon: socket listener + input box + answer card + pointer overlay + Claude session.

This is the shared controller. The windows, screenshots and socket come from the
platform layer: flippy/linux/ui.py (GTK + layer-shell) or flippy/mac/ui.py (AppKit).
The UI runs on the main thread; the Agent SDK client lives on an asyncio loop in a
worker thread. Results come back to the main thread through loop.idle_add.
"""
import asyncio
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time

from . import act_memory, errors, history_store, loop, onboarding, settings, setup_gate, themes, tips, updates, video, watch
from .brain import BrainError
from .providers import Brain, ProviderChoiceRequired
from .codex_provider import CodexError
from .frames import prepare_frame
from .requests import Request
from .usage_guard import PlanLimitReached
from .profile import current, Instance
from .actions import (BACKGROUND_CATALOG, BACKGROUND_PROMPT, ActionError, AppFrame, ApprovalPolicy, DesktopTools,
                      Screenshot, app_id, approval_text)
from .point import image_to_logical, segments
from .playback import Playback, Options
from .diagnostics import Diagnostics
from . import demos

PROFILE = current()
RUNTIME_DIR = PROFILE.runtime_dir
SOCK_PATH = PROFILE.socket
ASK_TIMEOUT_S = 90
IDLE_RESET_S = 15 * 60      # fresh Claude session after this much idle (keeps context/usage small)
DRAW_TIMEOUT_S = 60         # leave draw mode (and give the mouse back) if nothing happens
NOT_HERE = "not available on this platform yet (see docs/linux-port.md)"
TIPS_RETRY_S = 300          # after a failed tip deck write, wait this long before trying again
# Other knobs (model, effort, theme, pointer, timing) live in flippy/settings.py.


EVENTS_PATH = PROFILE.events
diagnostics = Diagnostics(EVENTS_PATH)
LISTENERS = []  # raw in-memory UI events remain available to the onboarding tour


def log(*args):
    """Operational lines for ~/Library/Logs/flippy.log (why a task stopped, update checks, help mode...).
    Never pass a question, an answer or typed text here: those stay out of the log (see flippy/diagnostics.py)."""
    print(time.strftime("%H:%M:%S"), *args, file=sys.stderr, flush=True)


def event(name, **data):
    diagnostics.event(name, **data)
    for fn in tuple(LISTENERS):
        try:
            fn(name, data)
        except Exception:
            log("event listener failed")


class Flippy:
    def __init__(self, ui):
        self.instance = Instance(PROFILE).acquire()
        self.ui = ui
        self.overlay = ui.overlay
        self.overlay.on_draw_done = self._draw_done
        self.overlay.on_draw_cancel = self.cancel_draw
        self.overlay.on_control = self.control
        self.box = ui.input_box(self.submit, self.dismiss)
        self.marked = False      # next question is about what the user drew
        self.gen = 0
        self.request = None
        self.input_disabled = False
        self.input_worker = None
        self.quitting = False
        self.followup_token = None
        # Give Python's OS signal handlers a bounded main-thread wakeup while idle.
        loop.timeout_add(250, lambda: not self.quitting)
        self.background_futures = set()
        self.fade_animation_id = 0
        self.play = None         # playback state of the reply being shown (see _play_tick)
        self.play_id = 0
        self.draw_timeout_id = 0
        self.shooter = ui.screenshotter
        self.busy = False
        self.action_tools = None
        self.action_future = None
        self.allowed_apps = set(settings.get("act", "allowed_apps"))
        self.tutorial = False    # the current answer is a tutorial: its ":click" steps wait for their click
        self.act_hold = None     # the question could become a desktop task: the reply's start, held until it isn't [ACT]
        self.act_question = None
        self.fade_id = 0
        self.fading = False
        self.last_ask = 0.0

        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.brain = Brain()
        self._apply_claude_settings()
        self._apply_theme()
        settings.on_change(self._on_setting)
        loop.timeout_add(60_000, lambda: self._update_tick() and False)  # first check a minute in, then every 6 h
        loop.timeout_add(6 * 3600 * 1000, self._update_tick)
        self.watcher = watch.Watcher()
        self.dealer = tips.Dealer()
        self.decks = {}          # app id -> tips.Deck
        self.writing = set()     # apps whose tips are being written
        self.tips_failed = {}    # app id -> when writing its tips last failed
        self.watch_id = 0
        self.sampling = False
        self._apply_help_settings()
        self.video = video.Review(self)
        self.tour = onboarding.Tour(self)
        LISTENERS.append(self.tour.on_event)
        loop.timeout_add(3000, self._maybe_start_tour)
        self._run(self.brain.start(), self._started, provider_bound=True)
        self.connection_future = None
        loop.timeout_add(5000, self._connection_tick)

        ui.listen(SOCK_PATH, self.command)
        os.chmod(SOCK_PATH, 0o600)
        self.instance.listening()
        log(f"listening on {SOCK_PATH}")

    def _started(self, result, err):
        log("claude session ready" if not err else f"session start failed: {err}")
        self._open_setup_if(setup_gate.launch_reason)  # the login check is done: no race with it

    def _open_setup_if(self, reason_of, *args):
        """Open setup when reason_of(...) gives a reason: a new install, a new major version, a lost login."""
        from .providers import CONNECTION_STATUS
        if not hasattr(self.ui, "open_setup") or getattr(self.ui, "setup_win", None):
            return
        reason = reason_of(*args, dict(CONNECTION_STATUS), settings.get("provider", "mode"))
        if not reason:
            return
        log(f"setup: opening ({reason if isinstance(reason, str) else 'login needed'})")
        self.ui.open_setup()
        if reason in ("install", "major"):
            try:
                setup_gate.mark_seen()
            except OSError as e:
                log(f"setup: couldn't record it was shown ({type(e).__name__})")

    # --- plumbing ---
    def _connection_tick(self):
        if self.quitting:
            return False
        # only while the setup window is open, to keep its marks live; a lost login is caught at launch and
        # when a request fails (flippy/setup_gate.py), not by checking `claude auth status` forever
        if not self.busy and getattr(self.ui, "setup_win", None):
            if self.connection_future is None or self.connection_future.done():
                self.connection_future = self._run(self.brain.connections(), lambda r, e: None)
        return True


    def _maybe_start_tour(self):
        if self.quitting:
            return False
        if self.busy or self.box.visible or self.overlay.drawing or self.overlay.showing:
            return True
        return self.tour.maybe_start()

    def _run(self, coro, cb, timeout=None, provider_bound=False):
        """provider_bound: work for the current model/provider (the session start), stopped when they change.
        The rest (tips, login checks) finishes, and picks up new settings on its next run."""
        if timeout:
            coro = asyncio.wait_for(coro, timeout)
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        fut.provider_bound = provider_bound
        self.background_futures.add(fut)

        def done(f):
            self.background_futures.discard(f)
            if self.quitting:
                return
            err = asyncio.CancelledError() if f.cancelled() else f.exception()
            loop.idle_add(lambda: cb(None if err else f.result(), err) and False)
        fut.add_done_callback(done)
        return fut

    def command(self, cmd):
        if PROFILE.demo and (cmd == "update" or cmd.startswith("update ")):
            return "Updates are disabled in Flippy Demo; rebuild the demo instead."
        if cmd == "doctor":
            return json.dumps({"profile": PROFILE.name, "version": updates.current_version(),
                               "root": os.path.realpath(os.path.dirname(os.path.dirname(__file__))),
                               "pid": os.getpid(), "app": os.environ.get("FLIPPY_APP"),
                               "busy": self.busy, "input_disabled": self.input_disabled,
                               "last_action": getattr(self, "last_action", None),
                               "permissions": self.ui.permission_state() if hasattr(self.ui, "permission_state") else {}})
        if cmd.startswith("quit-owned "):
            if os.path.realpath(cmd[11:]) != os.path.realpath(os.path.dirname(os.path.dirname(__file__))):
                return "refused: daemon belongs to another checkout"
            self.quit()
            return "ok"
        if not cmd.strip():
            return "unknown command"
        diagnostics.command(cmd)
        handled, result = demos.dispatch(self, cmd, event, log)
        if handled:
            return result
        if cmd == "ask":
            self.open_box()
        elif cmd == "draw":
            self.start_draw()
        elif cmd == "tour" or cmd.startswith("tour "):  # the first-run tour (flippy/onboarding.py)
            return self.tour.command(cmd)
        elif cmd == "video" or cmd.startswith("video "):  # video review (flippy/video.py)
            if not hasattr(self.ui, "video_window"):
                return NOT_HERE
            return self.video.command(cmd)
        elif cmd == "settings":
            self.open_settings()
        elif cmd == "setup" and hasattr(self.ui, "open_setup"):  # first-run setup
            self.ui.open_setup()
        elif cmd == "history" and hasattr(self.ui, "open_app"):  # the app window's History
            self.ui.open_app("history")
        elif cmd.startswith("control "):  # same as clicking a player button: control <name> [0-1 for seek/speed]
            parts = cmd.split()
            self.control(parts[1], float(parts[2]) if len(parts) > 2 else 0.0)
        elif cmd.startswith("set "):  # set <section.key> <value>, e.g. `flippy-ask set look.theme y2k`
            return self.set_command(cmd[4:])
        elif cmd == "dismiss":
            self.dismiss()
        elif cmd == "reset":
            self.reset_session()
        elif cmd == "quit":
            self.quit()
        elif cmd == "pause-toggle":  # pause/resume the walkthrough on screen (the double-tap shortcut)
            self.pause_toggle()
        elif cmd == "update":  # check for a new release now (offers it, or says you're up to date)
            self.check_for_update(force=True)
            return "checking for updates"
        elif cmd == "update install":  # install the latest release without asking
            self.install_update(None)
            return "updating"
        elif cmd == "version":
            return updates.current_version()
        elif cmd == "ping":
            return "pong"
        elif cmd.startswith("help-mode "):  # help-mode off|quiet
            return self.set_command("help.mode " + cmd.split(maxsplit=1)[1])
        elif cmd.startswith("click "):  # click <x> <y> [double]: logical px, top-left origin (Settings: automation)
            return self.click_command(cmd[6:].split())
        elif cmd.split()[0] in ("move", "path", "type", "key", "tap"):  # more scripted input, same toggle
            return self.input_command(cmd)
        elif cmd.startswith("hotkey "):  # a real hotkey fired (the platform calls this): log it, then do it
            name = cmd.split()[1]
            event("hotkey", key=name, combo=settings.get("keys", name) if name in settings.DEFAULTS["keys"] else name)
            return self.command("pause-toggle" if name == "pause" else name)
        elif cmd.startswith("goal "):  # goal <app id> <what they want to do>: steers that app's tips
            parts = cmd.split(maxsplit=2)
            if len(parts) < 3:
                return "usage: goal <app id> <what you want to do>"
            self.set_goal(parts[1], parts[2])
        elif cmd.startswith("watch-app "):  # watch-app <app id>: toggle help mode for that app
            self.toggle_watch_app(cmd.split(maxsplit=1)[1])
        elif cmd.startswith("q "):  # ask without the box (scripting/testing)
            self.submit(cmd[2:].strip())
        elif cmd == "act" or cmd.startswith("act "):
            return self.act(cmd[4:].strip())
        else:
            return f"unknown command: {cmd}"
        return "ok"

    # --- flow ---
    def open_box(self):
        if self.busy:
            if self.action_tools:
                self.dismiss()  # the ask hotkey also stops an action task
                return
            if self.request and self.request.mode == "tutor":
                self.request.cancel(loop.source_remove)
                self._stop_playback()
                self._cancel_fade()
                self.overlay.clear()
                self._show_box()
            return
        if self.overlay.drawing:
            self.cancel_draw()
        if self.box.visible:  # hotkey again toggles the box closed
            self.box.hide()
            return
        self._cancel_fade()  # keep the last answer up while typing a follow-up
        self._stop_playback()
        self._show_box()

    def _show_box(self):
        event("box")
        self.box.show()

    def submit(self, question, tutorial=None):
        """tutorial: steps they have to do wait for their click (":click"). None = decide from the question."""
        if not question:
            return
        if self.busy:
            owner = self.request
            if not owner or owner.mode != "tutor":
                return
            owner.cancel(loop.source_remove)
            self._stop_playback()
            self.box.hide()
            token = object()
            self.followup_token = token
            def ready():
                if self.quitting or self.request is not owner or self.followup_token is not token:
                    return False
                if not owner.drained.is_set() or self.busy:
                    return True
                self.submit(question, tutorial)
                return False
            loop.timeout_add(25, ready)
            return
        if question == "/act" or question.startswith("/act "):
            status = self.act(question[4:].strip())
            if self.action_tools is None:
                self.box.hide()
                self._fail(status)
            return
        if question in ("/new", "/reset"):
            self.box.hide()
            self.reset_session()
            return
        if question == "/settings":
            self.box.hide()
            self.open_settings()
            return
        self.tutorial = wants_tutorial(question) if tutorial is None else tutorial
        event("ask", question=question, tutorial=self.tutorial)
        offer_act = self._can_infer_act()
        self.act_hold, self.act_question = ("", question) if offer_act else (None, None)
        req = self._begin_request("tutor")
        req.history = {"question": question, "app": self._front_name(), "ts": time.time()}
        self.box.hide()
        keep_marks = self.marked
        if keep_marks:
            question = ("[I drew a red mark on the screen around what I'm asking about. It's my "
                        "annotation, not part of the app.]\n" + question)
        question += ("\n\n(tutorial: mark the steps I have to do myself with :click)" if self.tutorial else
                     "\n\n(not a tutorial: just answer and point, no :click)")
        if offer_act:
            question += "\n(you can act: if I'm asking you to do this for me, reply [ACT] only)"
        self.marked = False
        self._run_request(req, self._ask(question, req, keep_marks=keep_marks), self._on_answer, ASK_TIMEOUT_S)

    def _front_name(self):
        """The app in front when a question was asked, for History (its name only)."""
        try:
            front = self.ui.app_front() if hasattr(self.ui, "app_front") else None
            return front[1] if front else ""
        except Exception:
            return ""

    def _remember(self, req, kind, raw=None, tools=None, result=None, outcome="completed", detail=""):
        """One History row (flippy/history_store.py): text only, the steps without their POINT tags. Never fails
        the request."""
        h = getattr(req, "history", None)
        if not h:
            return
        req.history = None
        answer, steps, app = "", (), None
        if raw is not None:
            segs = [seg for seg in segments(raw, True) if seg.text]
            answer = " ".join(seg.text for seg in segs)
            if len(segs) > 1:
                steps = [seg.text + (f"  \u2192 {seg.point.label}" if seg.point and seg.point.label else "") for seg in segs]
        if tools is not None:
            answer = (tools.failure or "") if outcome == "stopped" else (result if isinstance(result, str) else "")
            detail = f"{tools.actions} input{'s' if tools.actions != 1 else ''}"
            app = ", ".join(dict.fromkeys(getattr(tools, "app_names", {}).values())) or None
        provider = settings.get("provider", "mode")
        try:
            history_store.add(kind, h["question"], answer, steps, app if app is not None else h["app"],
                              provider, outcome, detail, ts=h["ts"])
        except Exception as e:
            log(f"history: couldn't save ({type(e).__name__})")

    def _can_infer_act(self):
        """Can a plain question turn into a desktop task ("play some jazz in Spotify"), without typing /act?
        Not for questions about a drawn mark, or while a video is recording."""
        return bool(settings.get("act", "infer") and hasattr(self.ui, "app_look") and not self.marked
                    and not self.input_disabled and not getattr(getattr(self, "video", None), "recording", False))

    def _begin_request(self, mode):
        review = getattr(self, "video", None)
        if review and review.recording and mode != "video-record":
            review.cancel()
        previous = getattr(self, "request", None)
        if previous:
            previous.cancel(loop.source_remove)
        self._stop_playback()
        self._cancel_fade()
        self.gen += 1
        req = Request(self.gen, mode)
        self.request = req
        self.busy = True
        event("request", request_id=req.identity, mode=mode, outcome="started")
        return req

    def _owns(self, req):
        return req.owns(getattr(self, "request", None)) and not getattr(self, "quitting", False)

    def _run_request(self, req, coro, cb, timeout=None):
        """Only the coroutine's finally can acknowledge cancellation cleanup."""
        from types import SimpleNamespace
        tasks = []
        def cancel():
            def stop():
                if tasks:
                    tasks[0].cancel()
            self.loop.call_soon_threadsafe(stop)
        req.future = SimpleNamespace(cancel=cancel)
        async def run():
            started = time.monotonic()
            result = error = None
            tasks.append(asyncio.current_task())
            try:
                if req.canceled.is_set():
                    coro.close()
                    raise asyncio.CancelledError()
                result = await asyncio.wait_for(coro, timeout) if timeout else await coro
            except BaseException as err:
                error = err
            finally:
                req.drained.set()
                event("request", request_id=req.identity, mode=req.mode,
                      outcome="canceled" if req.canceled.is_set() else "failed" if error else "completed",
                      duration_ms=round((time.monotonic() - started) * 1000))
                def done():
                    req.pending = False
                    if self.request is req:
                        self.busy = False
                        if self._owns(req):
                            cb(result, error, req)
                        elif req.mode == "action":
                            self.action_tools = self.action_future = None
                    return False
                loop.idle_add(done)
        future = asyncio.run_coroutine_threadsafe(run(), self.loop)
        return future

    @staticmethod
    def _unlink(path):
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass

    async def _capture_frame_once(self, req, keep_marks=False, targeted=False):
        running = asyncio.get_running_loop()
        future = running.create_future()
        def deliver(path, error, target, logical):
            if future.done() or not self._owns(req):
                self._unlink(path)
                if not future.done():
                    future.cancel()
            elif error or not path:
                self._unlink(path)
                future.set_exception(ActionError("Could not capture the screen. " + (
                    "Check Screen Recording permission." if sys.platform == "darwin"
                    else "Allow the screenshot when COSMIC asks.")))
            else:
                future.set_result((path, target, logical))
        def begin():
            self.overlay.clear(keep_marks=keep_marks)
            self.box.hide()
            if targeted:
                self.ui.action_card.hide()
                self.ui.hide_nudge()
            target = self.ui.action_state() if targeted else None
            logical = self.ui.screen_size()
            timer_box = []
            def take():
                if timer_box:
                    req.timers.discard(timer_box[0])
                if not future.done() and self._owns(req):
                    try:
                        self.shooter.take(lambda path, err: running.call_soon_threadsafe(deliver, path, err, target, logical))
                    except Exception:
                        running.call_soon_threadsafe(deliver, None, True, target, logical)
                elif not future.done():
                    running.call_soon_threadsafe(future.cancel)
                return False
            timer = loop.timeout_add(self.ui.hide_settle_ms, take)
            timer_box.append(timer)
            req.timers.add(timer)
        path = None
        try:
            await self._action_main(begin, req)
            path, target, logical = await future
            worker = asyncio.create_task(asyncio.to_thread(prepare_frame, path, settings.get("claude", "image"),
                                                           logical_size=logical, target=target))
            try:
                frame = await asyncio.shield(worker)
            except asyncio.CancelledError:
                await asyncio.shield(worker)
                raise
            if not self._owns(req):
                raise asyncio.CancelledError()
            if targeted and await self._action_main(self.ui.action_state, req) != target:
                raise ActionError("The foreground window or display changed. Start a new /act request.")
            return frame
        finally:
            if path is None and future.done() and not future.cancelled() and future.exception() is None:
                path = future.result()[0]
            future.cancel()
            self._unlink(path)

    def _acting(self, req, operation=None):
        if self._owns(req):
            self.overlay.show_text(f"Flippy is acting{': ' + operation if operation else ''} · {self._stop_hint()} to stop")

    def _stop_hint(self):
        """How to stop a desktop task, in words: the answer card's fonts have no ⌘ ⇧ ⌥ ⌃ glyphs."""
        label = self.ui.key_label("pause") if hasattr(self.ui, "key_label") else None
        if not label:  # pause shortcut off: the ask hotkey stops it too
            label = self.ui.key_label("ask") if hasattr(self.ui, "key_label") else None
        return plain_keys(label or "the pause shortcut")

    async def _capture_frame(self, req, keep_marks=False, targeted=False):
        # Opening an app can change its window during capture. Retry capture only,
        # never an input, and never reuse coordinates from an unsettled frame.
        for attempt in range(4 if targeted else 1):
            try:
                frame = await self._capture_frame_once(req, keep_marks, targeted)
                if targeted:
                    await self._action_main(lambda: self._acting(req), req)
                return frame
            except ActionError as error:
                if not targeted or "foreground" not in str(error).lower() or attempt == 3:
                    raise
                if not self._owns(req):
                    raise asyncio.CancelledError()
                await asyncio.sleep(.7)


    # --- explicit desktop tasks; ordinary asks never receive tools ---
    def act(self, question):
        if not question:
            return "usage: act <task>"
        if self.busy or self.video.recording:
            return "Flippy is busy; dismiss the current task first"
        if self.input_disabled:
            return "Input cleanup could not be confirmed. Restart Flippy before acting again."
        if not hasattr(self.ui, "app_look"):
            return "desktop tasks are available on macOS only for now"
        try:
            self.ui.app_preflight()
            self.action_app = self.ui.app_front()  # the app you were in: the task starts there
        except ActionError as error:
            return str(error)
        req = self._begin_request("action")
        req.history = {"question": question, "app": self.action_app[1] if self.action_app else "", "ts": time.time()}
        self.action_policy = ApprovalPolicy(settings.get("act", "mode"))
        limits = settings.ACT_LIMITS[self.action_policy.mode]
        self.box.hide()
        # Background: the task works on one app's window and controls (flippy/mac/ax.py), never the real pointer
        # or keyboard, so you keep using your Mac. (The pointer-driving path below, _capture_frame and
        # _action_perform, is kept for the borrow-the-mouse-while-you're-idle fallback.)
        tools = DesktopTools(lambda: self._app_look(req),
                             lambda name, args, shot: self._action_approve(name, args, shot, req),
                             lambda name, args, shot, cancel: self._app_perform(name, args, shot, cancel, req),
                             max_actions=limits.max_actions, max_text=limits.max_text,
                             approval_mode=self.action_policy.mode,
                             # a platform whose tools differ (COSMIC: no pointer-free clicks) brings its own
                             catalog=getattr(self.ui, "act_catalog", BACKGROUND_CATALOG),
                             prompt=getattr(self.ui, "act_prompt", BACKGROUND_PROMPT) + act_memory.brief())
        tools.max_turns = limits.max_turns
        self.action_tools = tools
        async def run():
            try:
                return await self.brain.act(question, tools)
            finally:
                if tools.cleanup_failed:
                    self.input_disabled = True
                tools.stop()
        self.action_future = self._run_request(req, run(),
            lambda r, e, owner: self._action_done(tools, owner, r, e), limits.timeout_seconds)
        self._acting(req)
        return "desktop task started; " + self.action_policy.mode + " approval mode"

    async def _app_look(self, req):
        if not self._owns(req):
            raise ActionError("Task stopped.")
        frame = AppFrame(*await asyncio.to_thread(self.ui.app_look, *self.action_app))
        if frame.target[1] != self.action_app[2]:  # the app reopened under a new process: follow it
            log(f"act: {self.action_app[1]} is now process {frame.target[1]}")
            self.action_app = (frame.target[0], self.action_app[1], frame.target[1])
        if self.action_tools and app_id(frame.target):
            self.action_tools.app_names[app_id(frame.target)] = self.action_app[1]
        return frame

    async def _app_perform(self, name, args, shot, cancel, req):
        # which tool each step used (names only, never the task's text), so a task that gives up shows where
        log("act: step " + name + (f" {args.get('action')}" if name in ("app_action", "media") else "")
            + (" (real pointer)" if args.get("real_pointer") else ""))
        what = "needs the pointer for a moment, waiting for you to pause" if args.get("real_pointer") else \
            name.replace("_", " ")
        if hasattr(self.ui, "act_borrow_note"):  # COSMIC: keys and clicks by position can need the window for a moment
            what = self.ui.act_borrow_note(name, args) or what
        await self._action_main(lambda: self._acting(req, what), req)
        if name == "use_app":
            self.action_app = await asyncio.to_thread(self.ui.app_open, args["name"], cancel)
            return
        return await asyncio.to_thread(self.ui.app_act, name, args, shot, cancel)

    async def _action_main(self, fn, req=None):
        running = asyncio.get_running_loop()
        future = running.create_future()
        def finish(value, error):
            if not future.done():
                future.set_exception(error) if error else future.set_result(value)
        def run():
            if future.done():
                return False
            if req is not None and not self._owns(req):
                running.call_soon_threadsafe(future.cancel)
                return False
            try:
                value, error = fn(), None
            except Exception as err:
                value, error = None, err
            running.call_soon_threadsafe(finish, value, error)
            return False
        loop.idle_add(run)
        return await future

    async def _action_approve(self, name, args, shot, req):
        policy = self.action_policy
        scopeable = True
        if name in ("click", "scroll"):
            bounds = shot.target[3] if shot.target and len(shot.target) >= 5 else None
            x, y = shot.to_logical(args["x"], args["y"])
            scopeable = bool(bounds and bounds[0] <= x < bounds[0] + bounds[2]
                             and bounds[1] <= y < bounds[1] + bounds[3])
        elif name == "key" and args.get("combo") == "cmd+space":
            scopeable = False  # OS navigation has no known destination app yet.
        per_app = scopeable and policy.mode != "every_input" and not policy.sensitive(shot.target)
        if name == "drag":
            bounds = shot.target[3] if shot.target and len(shot.target) >= 5 else None
            endpoints = (shot.to_logical(args["x"], args["y"]), shot.to_logical(args["to_x"], args["to_y"]))
            if not bounds or any(not (bounds[0] <= x < bounds[0] + bounds[2]
                                      and bounds[1] <= y < bounds[1] + bounds[3]) for x, y in endpoints):
                raise ActionError("Both drag endpoints must be inside the foreground window.")
        if scopeable and not policy.needs_approval(shot.target, remembered=settings.get("act", "allowed_apps")):
            return self._owns(req)
        if name == "use_app" and policy.mode != "every_input":
            # Opening or switching to an app sends no input to anything, even when the task started in a terminal or
            # browser; what it then does in that app is approved by that app's own rules.
            return self._owns(req)
        running = asyncio.get_running_loop()
        future = running.create_future()
        def chosen(allowed, remember=False):
            if allowed and self._owns(req):
                if per_app:
                    policy.grant(shot.target)
                if remember:
                    app = shot.target[0]
                    try:
                        settings.set("act", "allowed_apps", sorted(set(settings.get("act", "allowed_apps")) | {app}))
                    except (OSError, ValueError):
                        self.overlay.show_text("Allowed for this task; could not save the remembered app.", error=True)
            def finish():
                if not future.done():
                    future.set_result(bool(allowed) and self._owns(req))
            running.call_soon_threadsafe(finish)
        def show():
            if name in ("click", "scroll", "drag"):
                x, y = shot.to_logical(args["x"], args["y"])
                label = "proposed " + name
                if name == "drag":
                    end = shot.to_logical(args["to_x"], args["to_y"])
                    bounds = shot.target[3] if shot.target and len(shot.target) >= 5 else None
                    if not bounds or any(not (bounds[0] <= px < bounds[0] + bounds[2]
                                              and bounds[1] <= py < bounds[1] + bounds[3])
                                         for px, py in ((x, y), end)):
                        raise ActionError("Both drag endpoints must be inside the foreground window.")
                    label += f" to ({end[0]:.0f}, {end[1]:.0f})"
                    self.overlay.strokes = [[(x, y), end]]
                    self.overlay.queue_draw()
                self.overlay.point(x, y, label)
            app = shot.target[0] if shot.target else "this app"
            if shot.target and hasattr(self.ui, "app_name"):
                app = self.ui.app_name(*shot.target[:2]) or app  # "Notes", not com.apple.Notes
            buttons = [("Stop", lambda: chosen(False)), ("Allow once", lambda: chosen(True))]
            if per_app:
                buttons = [("Stop", lambda: chosen(False)), (f"Allow {app}", lambda: chosen(True)),
                           (f"Always allow {app}", lambda: chosen(True, True))]
            self.ui.action_card.card(f"Allow Flippy to use {app} for this task?" if per_app else "Allow Flippy to act?",
                "Subsequent inputs in this app run until the task finishes or you stop it." if per_app else
                (shot.describe(name, args) if hasattr(shot, "describe") else approval_text(name, args)), buttons,
                on_timeout=lambda: chosen(False), timeout_s=60, width=480)
        try:
            await self._action_main(show, req)
            return await future
        finally:
            future.cancel()
            loop.idle_add(lambda: self.request is req and self.ui.action_card.hide() and False)

    async def _action_perform(self, name, args, shot, cancel, req):
        fresh = await self._capture_frame(req, targeted=True)
        if fresh.target != shot.target:
            raise ActionError("The foreground window or display changed. Take a new screenshot before acting.")
        # Clicks check the spot they aim at. Typing and shortcuts only compare the whole window when this exact
        # input was approved on a card: between steps the app is expected to change (Notes just made the note),
        # and the same-app/window check above still stops input that would land somewhere else.
        policy = getattr(self, "action_policy", None)
        exact = policy is None or policy.mode == "every_input" or policy.sensitive(shot.target)
        if (name in ("click", "scroll", "drag") or exact) and shot.substantial_change(fresh, name, args):
            raise ActionError("The screen changed before Flippy could act. Start a new /act request.")
        await self._action_main(lambda: self._acting(req, name), req)
        worker = asyncio.create_task(asyncio.to_thread(self.ui.action_input, name, args, fresh, cancel))
        self.input_worker = worker
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancel.set()
            try:
                await asyncio.wait_for(asyncio.shield(worker), 5)
            except (Exception, asyncio.CancelledError):
                self.input_disabled = True
                raise ActionError("Input cleanup could not be confirmed. Restart Flippy before acting again.") from None
            raise
        finally:
            if worker.done():
                self.input_worker = None

    def _action_done(self, tools, req, result, error):
        if not self._owns(req) or self.action_tools is not tools:
            return
        failure = (tools.failure or "").lower()
        stopped = bool(error or tools.failure)
        reason = "operation_failed" if stopped else "none"
        if getattr(tools, "failure_code", None):  # the refusal said what it was
            reason = tools.failure_code
        elif "screen changed" in failure:
            reason = "pixels_changed"
        elif "foreground" in failure:
            reason = "target_changed"
        elif "capture" in failure:
            reason = "capture_failed"
        elif "declined" in failure:
            reason = "declined"
        elif tools.cleanup_failed or "cleanup could not" in failure or "release could not" in failure:
            reason = "cleanup_failed"
        elif "accessibility" in failure or "permission" in failure:
            reason = "permission"
        # why it ended: reason codes and Flippy's own fixed refusal texts only, never the task or Claude's reply
        globals().get("log", lambda *a: None)(f"act: ended after {tools.actions} input(s), {reason}"
                                              + (f": {tools.failure}" if tools.failure else ""))
        self.last_action = {"request_id": req.identity, "completed_inputs": tools.actions,
                            "stopped": stopped, "cleanup_failed": tools.cleanup_failed,
                            "reason_code": reason}
        event("action_result", request_id=req.identity, count=tools.actions,
              outcome="stopped" if stopped else "completed", reason_code=reason)
        self.action_tools = self.action_future = None
        try:  # what worked in which app, for the next task (flippy/act_memory.py)
            act_memory.record(tools.journal, tools.notes, tools.app_names, finished=not stopped)
        except Exception as err:
            log(f"act: couldn't save what worked ({type(err).__name__})")
        self.ui.action_card.hide()
        self._remember(req, "task", tools=tools, result=result, outcome="stopped" if stopped else "completed")
        if stopped:
            code = errors.ACT_REASONS.get(reason, "ACT-STOPPED")
            if error is not None and not tools.failure:  # the provider ended it: its own code
                code = errors.code_of(error)
                log("act: error", errors.describe(error)[1])
            if isinstance(error, PlanLimitReached):
                message = errors.say(code, str(error))
            elif reason == "operation_failed" and tools.failure:
                # Flippy's own fixed refusal says what happened ("Action limit reached...", "Spotify quit...")
                message = errors.say(code, tools.failure.removeprefix("Desktop task stopped. ")
                                     .removeprefix("Task stopped: ").replace(" Check the screen before continuing.", ""))
            else:
                message = errors.say(code)
            self._fail("Desktop task stopped. " + message)
            if error:
                self._open_setup_if(setup_gate.login_error, error)
        else:
            self.overlay.show_text(result)
            self._schedule_fade(settings.get("timing", "max_show_seconds"))

    async def _ask(self, question, req, keep_marks=False):
        frame = await self._capture_frame(req, keep_marks=keep_marks)
        await self._action_main(lambda: (self.overlay.show_text("thinking…", phase="thinking"), event("thinking")), req)
        if self.brain.dirty or (self.last_ask and time.monotonic() - self.last_ask > IDLE_RESET_S):
            await self.brain.reset()
        self.last_ask = time.monotonic()
        await self._action_main(lambda: self._start_playback(req.identity, frame.size, frame.original_size, frame=frame), req)
        raw = await self.brain.ask(question, frame.jpeg, frame.size,
            on_text=lambda delta: loop.idle_add(lambda: self._owns(req) and self._stream_text(req.identity, delta) and False))
        return raw, req.identity

    def _on_answer(self, result, err, owner):
        if not self._owns(owner):
            return
        if err:
            self.act_hold = None
            self._stop_playback()
            shown, logged = errors.describe(err)
            log("error", logged)  # the code and the error's first line, never the question
            self._remember(owner, "ask", outcome="error", detail=shown)
            self._fail(shown)
            self._open_setup_if(setup_gate.login_error, err)
            return
        raw, gen = result
        held, self.act_hold = self.act_hold, None
        if held is not None and raw.lstrip().startswith(ACT_TAG):
            # They asked Flippy to do it, not how: the same words become a desktop task, with its approvals.
            event("answer", outcome="act")
            self._stop_playback()
            status = self.act(self.act_question)
            if self.action_tools is None:
                self._fail(status)
            return
        event("answer", outcome="completed")
        self._remember(owner, "ask", raw=raw)
        if self.play and self.play.gen == gen:
            if held:
                self.play.append(held)
            self.play.finish(raw)

    # --- playback: show the reply step by step, moving the hand to each point ---
    # The player skins' controls (control()) can pause, step back/forward, seek and replay.
    def _play_options(self):
        return Options(speed=settings.get("timing", "speed"), pace=settings.get("timing", "step_pace"),
                       tutorial=self.tutorial, clicks_available=hasattr(self.ui, "watch_clicks"),
                       wait_for_clicks=settings.get("timing", "wait_for_clicks"),
                       typing_available=hasattr(self.ui, "key_idle_s"),
                       pointer_size=settings.get("look", "pointer_size"),
                       show_seconds=settings.get("timing", "show_seconds"),
                       max_show_seconds=settings.get("timing", "max_show_seconds"))

    def _start_playback(self, gen, img_size, shot_size, frame=None):
        if gen != self.gen:
            return
        self._stop_playback()
        self.play = Playback(time.monotonic())
        self.play.gen, self.play.img, self.play.shot, self.play.frame = gen, img_size, shot_size, frame
        self._resume_ticking()

    def _stream_text(self, gen, delta):
        if not (self.play and self.play.gen == gen):
            return
        if self.act_hold is not None:
            self.act_hold += delta
            start = self.act_hold.lstrip()
            if ACT_TAG.startswith(start) or start.startswith(ACT_TAG):
                return  # could still be [ACT]: show nothing yet
            delta, self.act_hold = self.act_hold, None
        self.play.append(delta)

    def _stop_playback(self):
        if self.play:
            self._play_events(self.play.stop())
        if self.play_id:
            loop.source_remove(self.play_id)
            self.play_id = 0
        self.play = None

    def _resume_ticking(self):
        if self.play and not self.play_id:
            self.play.last = time.monotonic()
            self.play_id = loop.timeout_add(33, self._play_tick)

    def _play_coords(self, point):
        pl = self.play
        if pl.frame:
            return pl.frame.to_logical(point.x, point.y, clamp=True)
        return image_to_logical(point, pl.img, pl.shot, self._scale())

    def _play_events(self, events):
        for kind, payload in events:
            if kind == "point":
                (x, y), point, text = payload
                self.overlay.point(x, y, point.label)
                event("point", x=x, y=y, label=point.label, text=text)
            elif kind == "watch_clicks":
                self._listen_for_click(payload)
            elif kind == "waiting":
                (x, y), point = payload
                event("waiting", x=x, y=y, label=point.label)
            elif kind == "continue":
                self._continue_tutorial()
            elif kind in ("finish", "fade"):
                if kind == "finish":
                    event("answer_done")
                if self.play and not self.play.paused:
                    self._schedule_fade(payload)
            elif kind == "cancel_fade":
                self._cancel_fade()
            elif kind == "empty":
                self.overlay.show_text(payload, progress=1.0, finished=True, controls=True)

    def _play_tick(self):
        pl = self.play
        if pl is None:
            self.play_id = 0
            return False
        now = time.monotonic()
        idle = self.ui.key_idle_s() if hasattr(self.ui, "key_idle_s") else None
        events = pl.tick(now, self._play_options(), self._play_coords, idle)
        self._play_events(events)
        if self.play is not pl or pl.stopped:
            return False
        self._render_step(pl, None, now)
        if pl.finished:
            self.play_id = 0
            return False
        return True

    def _render_step(self, pl, _segs, now):
        card = pl.card(now, self._play_options())
        if card:
            card["at_bottom"] = self._card_at_bottom(pl.known())
            self.overlay.show_text(**card)

    def _listen_for_click(self, on):
        if hasattr(self.ui, "watch_clicks"):
            self.ui.watch_clicks(self._on_user_click) if on else self.ui.unwatch_clicks()

    def _on_user_click(self, x, y):
        if self.play and self.play.click(x, y, time.monotonic(), self._play_options()):
            event("user_click", x=x, y=y)

    def _continue_tutorial(self):
        event("tutorial_continue")
        self._stop_playback()
        self.submit("Done, I did that. Continue the walkthrough from here.", tutorial=True)

    def _card_at_bottom(self, segs):
        for seg in segs:
            if seg.point:
                _, y = self._play_coords(seg.point)
                return y < self.ui.screen_size()[1] / 2
        return False

    def control(self, name, frac=0.0):
        event("control", control=name)
        self.overlay.pressed = (name, time.monotonic())
        if name in ("stop", "close", "min"):
            self.dismiss()
            return
        if name == "speed":
            settings.set("timing", "speed", themes.frac_to_speed(frac))
            return
        if name == "speed_cycle":
            options = [0.5, 0.75, 1.0, 1.5, 2.0]
            cur = settings.get("timing", "speed")
            settings.set("timing", "speed", next((o for o in options if o > cur + 0.01), options[0]))
            return
        if self.play:
            self._play_events(self.play.control(name, frac, time.monotonic(), self._play_options()))
            if self.play and not self.play.stopped:
                self._resume_ticking()
                self._refresh_card()

    def pause_toggle(self):
        """Pause or resume the answer playing on screen; nothing if there isn't one (or it already finished).
        During a desktop task (/act) the same gesture (double-tap ⌘, or the pause shortcut) stops the task."""
        if getattr(self, "action_tools", None):
            log("act: stopped with the pause gesture")
            event("control", control="stop")
            self.dismiss()
            return
        if self.play and not self.play.finished:
            self.control("play" if self.play.paused else "pause")

    def _set_paused(self, paused):
        self.control("pause" if paused else "play")

    def _goto(self, k, retype=False):
        if self.play:
            self._play_events(self.play._goto(k, time.monotonic(), retype))
            self._resume_ticking()

    def _refresh_card(self):
        if self.play:
            self._render_step(self.play, None, time.monotonic())
        elif self.overlay.card:
            self.overlay.card.speed = settings.get("timing", "speed")
        self.overlay.queue_draw()

    def _fail(self, msg):
        self.busy = False
        self._stop_playback()
        log("error:", msg)
        self.overlay.clear()
        self.overlay.show_text(msg, error=True)
        self._schedule_fade(settings.get("timing", "show_seconds"))

    def reset_session(self):
        previous = self.request
        self.dismiss()
        async def reset():
            if previous and previous.pending:
                await asyncio.to_thread(previous.drained.wait)
            await self.brain.reset()
        def begin():
            req = self._begin_request("reset")
            self.overlay.show_text("starting a fresh session…")
            def done(_result, error, owner):
                if error:
                    self._fail("Could not reset the session.")
                else:
                    self.overlay.show_text("fresh session ready")
                    self._schedule_fade(2)
            self._run_request(req, reset(), done)
            return False
        if previous and previous.pending:
            def ready():
                if self.quitting:
                    return False
                if not previous.drained.is_set():
                    return True
                begin()
                return False
            loop.timeout_add(25, ready)
        else:
            begin()

    def dismiss(self):
        self.followup_token = None
        req = getattr(self, "request", None)
        if self.action_tools:
            self.action_tools.stop()
            self.ui.action_card.hide()
        if req:
            req.cancel(loop.source_remove)
            if req.mode == "action":
                # A stopped desktop task can take a few seconds to close its Claude session. Each task has its own
                # session and stop signal, and a canceled request's results are already ignored, so don't make
                # the user wait for that: free Flippy now and let the old task finish closing in the background.
                # (Questions keep waiting: they share one conversation.)
                self.busy = False
                self.action_tools = self.action_future = None
        self.gen += 1
        self.box.hide()
        self._stop_playback()
        self._cancel_fade()
        self.marked = False
        if self.video.recording:
            self.video.cancel()
        if self.overlay.drawing:
            self.cancel_draw()
        else:
            self.overlay.clear()

    def quit(self):
        if self.quitting:
            return
        self.quitting = True
        self.dismiss()
        self.video.cancel()
        for future in tuple(self.background_futures):
            future.cancel()
        req = self.request
        async def stop():
            if req and req.pending:
                await asyncio.to_thread(req.drained.wait)
            worker = self.input_worker
            if worker is not None:
                try:
                    await asyncio.shield(worker)
                except Exception:
                    pass  # worker finished; its cleanup-failure lockout remains visible
            await self.brain.stop()
            loop.idle_add(lambda: self.ui.quit() and False)
        asyncio.run_coroutine_threadsafe(stop(), self.loop)

    # --- help mode: offer a hand when they seem stuck (flippy/watch.py) ---
    def _apply_help_settings(self):
        self.watcher.apps = settings.get_list("help", "apps")
        self.watcher.muted = settings.get_list("help", "muted")
        on = settings.get("help", "mode") != "off" and hasattr(self.ui, "sample")
        if on and not self.watch_id:
            self.watch_id = loop.timeout_add(int(watch.SAMPLE_S * 1000), self._watch_tick)
            log(f"help mode on, watching {sorted(self.watcher.apps) or 'no apps yet'}")
        elif not on and self.watch_id:
            loop.source_remove(self.watch_id)
            self.watch_id = 0
            self.watcher.reset()
            self.ui.hide_nudge()
            log("help mode off")

    def toggle_watch_app(self, app):
        apps = settings.get_list("help", "apps")
        apps.symmetric_difference_update({app})
        settings.set_list("help", "apps", apps)
        if app in apps and settings.get("help", "mode") == "off":
            settings.set("help", "mode", "quiet")  # picking an app to watch turns help mode on

    def _watch_tick(self):
        if (self.busy or self.box.visible or self.overlay.showing or self.overlay.drawing
                or self.ui.nudge_visible() or self.sampling):
            self.watcher.reset()  # Flippy's own stuff on screen isn't the person being stuck
            self.dealer.reset()
            return True
        if not self.watcher.apps:
            return True
        self.sampling = True

        def work():  # the screen thumbnail takes ~100 ms: off the main thread
            try:
                s = self.ui.sample()
            except Exception as e:
                log(f"help mode: sampling failed: {e}")
                s = None
            loop.idle_add(lambda: self._watch_feed(s) and False)
        threading.Thread(target=work, daemon=True).start()
        return True

    def _watch_feed(self, sample):
        self.sampling = False
        if sample is None or self.busy or self.box.visible or self.overlay.showing:
            return
        offer = self.watcher.feed(sample)
        tip_time = settings.get("help", "mode") == "tips" and self.dealer.feed(sample, self.watcher.apps,
                                                                               self.watcher.muted)
        if offer:
            log(f"help mode: offering a hand in {offer.app_name} ({offer.reason})")
            event("nudge", app=offer.app, reason=offer.reason)
            self.dealer.shown(sample.t)  # no tip right on the heels of an offer
            self.ui.show_nudge(offer, lambda: self._nudge_help(offer), lambda: self.watcher.not_now(offer.app),
                               lambda: self._nudge_mute(offer))
            return
        if settings.get("help", "mode") == "tips" and sample.app in self.watcher.apps:
            deck = self._deck(sample.app, sample.app_name)
            tip = deck.next_tip() if tip_time else None
            if tip:
                self.dealer.shown(sample.t)
                self._show_tip(deck, tip)

    # --- tips mode (flippy/tips.py): a cached deck per app, dealt out locally ---
    def _deck(self, app, name):
        deck = self.decks.get(app) or tips.Deck.load(app) or tips.Deck(app, name or app)
        if name and deck.app_name != name:
            deck.app_name = name
        self.decks[app] = deck
        if deck.needs_refill():
            self._write_tips(deck)
        return deck

    def _write_tips(self, deck):
        if deck.app in self.writing or time.monotonic() - self.tips_failed.get(deck.app, -1e9) < TIPS_RETRY_S:
            return
        self.writing.add(deck.app)
        n = tips.REFILL_SIZE if deck.tips else tips.DECK_SIZE
        log(f"tips: writing {n} tips for {deck.app_name}" + (f" (goal: {deck.goal})" if deck.goal else ""))

        def done(new, err):
            self.writing.discard(deck.app)
            if err:
                log(f"tips: couldn't write tips for {deck.app_name}: {err}")
                self.tips_failed[deck.app] = time.monotonic()
                return
            before = len(deck.tips)
            deck.add(new)
            deck.save()
            log(f"tips: {len(deck.tips) - before} new tips for {deck.app_name}")
        self._run(self.brain.write_tips(deck.app_name, deck.goal, n, deck.texts()), done, timeout=180)

    def _show_tip(self, deck, tip):
        log(f"tips: showing a level-{tip['level']} tip in {deck.app_name}")
        event("tip", app=deck.app, text=tip["text"])

        def mark(state):
            deck.mark(tip, state)
            if deck.app != "demo":
                deck.save()

        def show_me():
            mark("shown")
            self.submit(f"I'm learning {deck.app_name} and got this tip: \"{tip['text']}\" "
                        f"Show me where that is on my screen right now, step by step.", tutorial=True)
        self.ui.show_tip(deck.app_name, tip["text"], got_it=lambda: mark("shown"), knew=lambda: mark("knew"),
                         show_me=show_me)

    def set_goal(self, app, goal, name=None):
        """A new goal for an app: drop its unseen tips and write fresh ones for the goal."""
        deck = self.decks.get(app) or tips.Deck.load(app) or tips.Deck(app, name or app)
        if name:
            deck.app_name = name
        self.tips_failed.pop(app, None)
        deck.goal = goal.strip()
        deck.tips = [t for t in deck.tips if t["state"] != "new"]
        deck.save()
        self.decks[app] = deck
        self._write_tips(deck)

    def _nudge_help(self, offer):
        self.watcher.helped(offer.app)
        self.submit(offer.question(), tutorial=True)

    def _nudge_mute(self, offer):
        self.watcher.mute(offer.app)
        settings.set_list("help", "muted", settings.get_list("help", "muted") | {offer.app})

    # --- updates (flippy/updates.py): offer new GitHub Releases ---
    def _update_tick(self):
        if settings.get("updates", "check") and updates.due():
            self.check_for_update()
        return True

    def check_for_update(self, force=False):
        def work():
            try:
                rel, err = updates.check(force=force), None
            except updates.UpdateError as e:
                rel, err = None, e
            loop.idle_add(lambda: self._update_checked(rel, err, force) and False)
        threading.Thread(target=work, daemon=True).start()

    def _update_checked(self, rel, err, force):
        if err:
            log(f"update: check failed: {err}")
            if force:
                self._fail(f"Couldn't check for updates: {err}")
            return
        if rel is None:
            log(f"update: {updates.current_version()} is the latest")
            if force and not self.busy:
                self.overlay.show_text(f"Flippy {updates.current_version()} is up to date.")
                self._schedule_fade(3)
            return
        log(f"update: {rel['version']} is out (have {updates.current_version()})")
        if self.action_tools:
            return  # don't cover an action approval with the unrelated update card
        if hasattr(self.ui, "show_update"):
            self.ui.show_update(rel, install=lambda: self.install_update(rel),
                                later=lambda: updates.later(rel["version"]))
        else:
            log("update: run `flippy-ask update install` to install it")

    def install_update(self, rel):
        if self.busy:
            return
        self.busy = True
        self.overlay.show_text(f"Updating Flippy{' to ' + rel['version'] if rel else ''}…")

        def work():
            try:
                res, err = updates.install(log=log), None
            except (updates.UpdateError, OSError, subprocess.TimeoutExpired) as e:
                res, err = None, e
            loop.idle_add(lambda: self._update_installed(res, err) and False)
        threading.Thread(target=work, daemon=True).start()

    def _update_installed(self, res, err):
        self.busy = False
        if err:
            self._fail(f"Couldn't update: {err}")
            return
        log(f"update: {res['from']} -> {res['to']}" + (", packages updated" if res["packages"] else ""))
        if res["from"] == res["to"]:
            self.overlay.show_text("Already up to date.")
            self._schedule_fade(3)
            return
        self.overlay.show_text(f"Updated to {res['version']}. Restarting…")
        loop.timeout_add(1200, lambda: self.ui.restart(full_install=res["app"]) and False)

    # --- settings ---
    def open_settings(self):
        self.ui.open_settings()

    def click_command(self, args):
        """Scripted clicks for demos and automation. Off unless turned on in Settings; Claude never triggers these."""
        if not settings.get("automation", "clicks"):
            return "clicks are off: turn on Settings > Hotkeys > Let scripts click and type (automation.clicks)"
        if not hasattr(self.ui, "click"):
            return "clicks aren't supported on this platform yet"
        try:
            x, y = float(args[0]), float(args[1])
        except (IndexError, ValueError):
            return "usage: click <x> <y> [double]"
        log(f"click at {x:.0f},{y:.0f}" + (" (double)" if "double" in args[2:] else ""))
        return self.ui.click(x, y, double="double" in args[2:])

    def input_command(self, cmd):
        """move <x> <y> | path [drag] <points.json> | type <text> | key <combo> | tap <mod> [times]"""
        if not settings.get("automation", "clicks"):
            return "scripted input is off: turn on Settings > Hotkeys > Let scripts click and type (automation.clicks)"
        if not hasattr(self.ui, "type_text"):
            return NOT_HERE
        verb, _, rest = cmd.partition(" ")
        try:
            if verb == "move":
                x, y = map(float, rest.split()[:2])
                return self.ui.move(x, y)
            if verb == "path":
                drag = rest.startswith("drag ")
                with open(rest[5:] if drag else rest) as f:
                    points = [tuple(p) for p in json.load(f)]
                event("path", drag=drag, n=len(points))
                return self.ui.path(points, drag)
            if verb == "type":
                return self.ui.type_text(rest, on_key=lambda ch: event("typed", ch=ch))
            if verb == "key":
                event("keys", combo=rest.strip())
                return self.ui.key(rest.strip())
            if verb == "tap":
                mod, *n = rest.split()
                event("keys", combo=f"double-{mod}" if (int(n[0]) if n else 2) == 2 else mod)
                return self.ui.tap(mod, int(n[0]) if n else 2)
        except (ValueError, OSError, KeyError) as e:
            return f"bad {verb}: {e}"
        return "unknown"

    def set_command(self, arg):
        try:
            path, raw = arg.split(maxsplit=1)
            section, key = path.split(".")
            value = settings.parse_value(path, raw)
            settings.set(section, key, value)
            return "ok"
        except (ValueError, KeyError):
            return "invalid setting; usage: set <section.key> <value>"
        except OSError:
            return "could not save settings"

    def _on_setting(self, section, key, value):
        log("setting changed")
        if section in ("claude", "codex", "provider") and key in ("model", "effort", "mode"):
            if self.request and self.request.pending:
                self.dismiss()
            for future in tuple(self.background_futures):
                if future.provider_bound:
                    future.cancel()
            self._apply_claude_settings()
        elif section == "act" and key == "mode":
            if self.action_tools:
                self.dismiss()
        elif section == "act" and key == "allowed_apps":
            previous = getattr(self, "allowed_apps", set())
            self.allowed_apps = set(value)
            if previous - self.allowed_apps and self.action_tools:
                self.dismiss()
        elif section == "look" and key == "theme":
            self._apply_theme()
        elif section == "timing" and key == "speed":
            self._refresh_card()
        elif section == "help":
            self._apply_help_settings()
        self.overlay.queue_draw()

    def _apply_claude_settings(self):
        model, effort = settings.get("claude", "model"), settings.get("claude", "effort")
        self.brain.configure(None if model == "default" else model, effort)
        provider = settings.get("provider", "mode")
        if provider == "codex":
            model, effort = settings.get("codex", "model"), settings.get("codex", "effort")
        self.overlay.meta = f"{provider.upper() if model == 'default' else model.upper()} · {effort.upper()}"

    def _apply_theme(self):
        theme = themes.get(settings.get("look", "theme"))
        if hasattr(theme, "refresh"):
            theme.refresh()  # e.g. re-read COSMIC's colors
        self.ui.apply_theme(theme)

    def demo_point(self, *args, **kwargs):
        return demos.demo_point(self, *args, **kwargs)

    def preview(self, *args, **kwargs):
        return demos.preview(self, *args, **kwargs)

    # --- draw mode: circle something, then ask about it ---
    def start_draw(self):
        if self.busy:
            return
        if self.overlay.drawing:  # hotkey again = cancel
            self.cancel_draw()
            return
        self.box.hide()
        self._cancel_fade()
        self.overlay.clear()
        self.overlay.start_drawing()
        event("draw_start")
        self.overlay.show_text("draw around something, then let go to ask · Esc or right-click to cancel")
        self.draw_timeout_id = loop.timeout_add_seconds(DRAW_TIMEOUT_S, self._draw_timed_out)

    def _draw_timed_out(self):
        self.draw_timeout_id = 0  # this source is finishing; don't remove it again
        self.cancel_draw()
        return False

    def _end_draw_mode(self):
        if self.draw_timeout_id:
            loop.source_remove(self.draw_timeout_id)
            self.draw_timeout_id = 0
        self.overlay.stop_drawing()

    def _draw_done(self):
        pts = [p for st in self.overlay.strokes for p in st]
        if pts:
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            event("drawn", x0=min(xs), y0=min(ys), x1=max(xs), y1=max(ys))
        self._end_draw_mode()
        self.overlay.clear(keep_marks=True)  # drop the hint, keep the marks
        self.marked = True
        self._show_box()

    # --- scripted demo helpers (used by scripts/record_demo.py) ---
    def demo_type(self, *args, **kwargs):
        return demos.demo_type(self, *args, event=event, **kwargs)

    def demo_draw(self, *args, **kwargs):
        return demos.demo_draw(self, *args, **kwargs)

    def demo_pointer(self, *args, **kwargs):
        return demos.demo_pointer(self, *args, **kwargs)

    def cancel_draw(self):
        event("draw_cancel")
        self._end_draw_mode()
        self.marked = False
        self.overlay.clear()

    # --- fading ---
    def _schedule_fade(self, seconds):
        self._cancel_fade()
        owner, gen = getattr(self, "request", None), self.gen
        timer_box = []
        def fade():
            if owner and timer_box:
                owner.timers.discard(timer_box[0])
            if self.gen == gen and getattr(self, "request", None) is owner and self.fade_id == timer_box[0]:
                self.fade_id = 0
                self._fade_out()
            return False
        self.fade_id = loop.timeout_add(int(seconds * 1000), fade)
        timer_box.append(self.fade_id)
        if owner:
            owner.timers.add(self.fade_id)

    def _fade_timer(self):
        self.fade_id = 0
        self._fade_out()
        return False

    def _cancel_fade(self):
        for key in ("fade_id", "fade_animation_id"):
            timer = getattr(self, key, 0)
            if timer:
                loop.source_remove(timer)
                setattr(self, key, 0)
        self.fading = False
        self.overlay.set_opacity(1.0)

    def _fade_out(self):
        self._cancel_fade()
        self.fading = True
        start, gen, owner = time.monotonic(), self.gen, getattr(self, "request", None)
        def step():
            if self.gen != gen or getattr(self, "request", None) is not owner:
                return False
            if self.busy or self.box.visible:
                self.fading = False
                self.fade_animation_id = 0
                self.overlay.set_opacity(1.0)
                return False
            opacity = 1 - (time.monotonic() - start) / 0.4
            if opacity <= 0:
                self.fading = False
                self.fade_animation_id = 0
                self._stop_playback()
                self.overlay.clear()
                event("cleared")
                return False
            self.overlay.set_opacity(opacity)
            return True
        self.fade_animation_id = loop.timeout_add(16, step)
        if owner:
            owner.timers.add(self.fade_animation_id)

    def _scale(self):
        return self.ui.scale()


TUTORIAL_RE = re.compile(r"\b(how (do|can|would|should|to) (i|you|we)|how to|walk me|teach me|show me how|guide me|"
                         r"step[- ]by[- ]step|steps (to|for)|where do i (click|find|go)|help me (set up|make|create|add|"
                         r"build|record|install|do))\b")


# "now add a bass loop", "make it louder", "add a new track": asking to be walked through doing it
DO_RE = re.compile(r"^(?:(?:ok|okay|now|then|next|and|also|cool|nice)[,!]?\s+)*(?:let'?s\s+|can you\s+)?"
                   r"(?:add|make|create|put|record|build|insert|set up|turn (?:on|up|down|off)|open|change|switch|"
                   r"start|bring in|layer)\b")


ACT_TAG = "[ACT]"  # the tutor's whole reply when a question asks Flippy to do something (brain.SYSTEM_PROMPT)


KEY_WORDS = (("⌘", "Cmd "), ("⇧", "Shift "), ("⌥", "Option "), ("⌃", "Ctrl "))


def plain_keys(label):
    """"double-tap ⌘" -> "double-tap Cmd", "⇧⌘Space" -> "Shift Cmd Space": for text drawn in fonts without the symbols."""
    for sym, word in KEY_WORDS:
        label = label.replace(sym, word)
    return " ".join(label.split())


def wants_tutorial(question):
    """Is this a "how do I do X" / "now add X" request (a tutorial: steps wait for their clicks) or a question?"""
    q = question.lower().strip()
    return bool(TUTORIAL_RE.search(q) or DO_RE.search(q))


def main():
    diagnostics.record_content = "--demo-record-content" in sys.argv
    themes.load_fonts()  # bundled pixel font for the Y2K theme, process-local
    if sys.platform == "darwin":
        from .mac import ui
    else:
        from .linux import ui
    def create(native_ui):
        app = Flippy(native_ui)
        from .runtime import install_shutdown_handlers
        install_shutdown_handlers(app.quit, loop.idle_add)
        return app
    ui.run(create)


if __name__ == "__main__":
    main()
