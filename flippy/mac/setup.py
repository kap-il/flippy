"""First-run setup on macOS, for the app window's Setup page: Claude and Codex sign-in, Screen Recording,
Accessibility, open at login, restart. The page (flippy/shell/web/setup.js) shows state() and asks for act(name).

The page is shown by itself until every required step is done (flippy/setup_gate.py) and from the menu bar's
"Setup…". It re-reads state() every second while it's showing, so granting a permission in System Settings ticks
it off without any clicking.
"""
import json
import os
import plistlib
import subprocess

from AppKit import NSAlert, NSAlertFirstButtonReturn
from Quartz import CGPreflightScreenCaptureAccess, CGRequestScreenCaptureAccess

from .. import claude_login
from ..profile import current
from ..shell.setup import claude_cli, logged_in, login_items, run, terminal_env
from . import hotkeys

APP = os.environ.get("FLIPPY_APP")  # set by Flippy.app's launcher; None when run from a terminal
AGENT_LABEL = current().bundle_id
AGENT = os.path.expanduser(f"~/Library/LaunchAgents/{AGENT_LABEL}.plist")
SCREEN_PANE = "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
ACCESSIBILITY_PANE = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
_claude_failed = False


# ---- checks
def screen_ok():
    return bool(CGPreflightScreenCaptureAccess())


def needs_setup():
    from ..providers import setup_ready
    return not (setup_ready() and screen_ok())


def temporary_path(app):
    """Flippy running from the disk image or a quarantine copy (App Translocation): the path is gone after
    ejecting or a restart, so a login item there would open nothing."""
    return bool(app) and (app.startswith("/Volumes/") or "/AppTranslocation/" in app)


def installed():
    return bool(APP and os.path.isdir(APP)) and not temporary_path(APP)


def login_item_on():
    if current().demo:
        return False
    return os.path.exists(AGENT)


def state():
    """Everything the Setup page shows, as plain data."""
    app = os.path.abspath(APP) if installed() else None
    home = os.path.expanduser("~")
    return {
        "claude": logged_in("claude"), "codex": logged_in("codex"),
        "claude_waiting": claude_login.waiting(), "claude_failed": _claude_failed,
        "screen": screen_ok(), "accessibility": hotkeys.accessibility_trusted(),
        "login_item": login_item_on(), "login_item_allowed": installed() and not current().demo,
        "app": app and ("~" + app[len(home):] if app.startswith(home + os.sep) else app),
        "app_name": app and os.path.basename(app),
    }


# ---- actions
def confirm_claude_reconnect():
    alert = NSAlert.alloc().init()
    alert.setMessageText_("Claude is already connected")
    alert.setInformativeText_("Sign in again, for example with a different account? Claude's sign-in page opens in your browser.")
    alert.addButtonWithTitle_("Sign In Again")
    alert.addButtonWithTitle_("Cancel")
    return alert.runModal() == NSAlertFirstButtonReturn


def open_claude_login():
    """Use main's browser sign-in (#20), with the same reconnect confirmation and status."""
    global _claude_failed
    if logged_in("claude") and not claude_login.waiting() and not confirm_claude_reconnect():
        return
    _claude_failed = not claude_login.start(claude_cli())


def open_codex_login():
    # The owner completes Codex's native browser flow, into Flippy's own Codex home; Flippy never handles tokens.
    from ..codex_provider import codex_executable, login_command
    cli = codex_executable()  # not only PATH: apps get a minimal one
    if not cli:
        subprocess.Popen(["open", "https://developers.openai.com/codex/cli/"])
        return
    script = 'tell application "Terminal" to do script ' + json.dumps(login_command(cli))
    subprocess.Popen(["osascript", "-e", script], env=terminal_env())


def ask_screen():
    CGRequestScreenCaptureAccess()  # the system prompt (only the first time)
    subprocess.Popen(["open", SCREEN_PANE])


def ask_accessibility():
    from . import ax
    ax.ask_trust()  # macOS's prompt, and Flippy added to the list
    subprocess.Popen(["open", ACCESSIBILITY_PANE])


def restart():
    """macOS applies Screen Recording only to newly started processes."""
    from . import ui
    ui.PLATFORM.restart()


def reveal():
    if installed():
        from .permission_buddy import reveal_app
        reveal_app(APP)


def set_login_item(on):
    """A LaunchAgent that opens Flippy.app at login (no extra permission needed)."""
    if not on:
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{AGENT_LABEL}"], capture_output=True)
        try:
            os.unlink(AGENT)
        except FileNotFoundError:
            pass
        return
    if not APP or temporary_path(APP):
        return
    os.makedirs(os.path.dirname(AGENT), exist_ok=True)
    with open(AGENT, "wb") as f:
        plistlib.dump({"Label": AGENT_LABEL, "ProgramArguments": ["/usr/bin/open", "-g", APP], "RunAtLoad": True}, f)


ACTIONS = {
    "claude": open_claude_login, "codex": open_codex_login, "screen": ask_screen,
    "accessibility": ask_accessibility, "restart": restart, "reveal": reveal, **login_items(set_login_item),
}


def act(name):
    run(ACTIONS, name)
