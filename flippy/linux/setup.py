"""First-run setup on COSMIC, for the app window's Setup page: Claude and Codex sign-in, Flippy's keyboard
shortcuts, open at login. The page (flippy/shell/web/setup.js) shows state() and asks for act(name). The Linux
side of flippy/mac/setup.py, and no GTK: the window is flippy/linux/shell_window.py.

COSMIC has no permission settings to visit first (screenshots go through the portal), so no Screen Recording
step. /act does need two things: AT-SPI, Linux's accessibility layer (atspi.enable turns it on; apps already open
may need a restart to show their controls), and the RemoteDesktop portal, which COSMIC asks about once, the first
time /act clicks by position. Both need COSMIC Epoch 1.7 or newer. The shortcuts live in COSMIC's custom
shortcuts file: Flippy can add the missing ones there (backed up first), and COSMIC picks them up right away.
"""
import os
import re
import shlex
import shutil
import subprocess

from . import desktop
from ..profile import current
from ..shell.setup import claude_cli, logged_in, login_items, run, terminal_env

ASK = os.path.expanduser("~/.local/bin/flippy-ask")
SHORTCUTS = os.path.expanduser("~/.config/cosmic/com.system76.CosmicSettings.Shortcuts/v1/custom")
# COSMIC's own terminal first; each takes the command to run after these
TERMINALS = (["cosmic-term", "-e"], ["gnome-terminal", "--"], ["x-terminal-emulator", "-e"], ["kgx", "--"],
             ["xterm", "-e"])

# (what, flippy-ask args, COSMIC modifiers, key or None)
WANTED = [("Ask about the screen", "", ["Super", "Shift"], "space"),
          ("Circle something, then ask", "draw", ["Super", "Alt"], None),
          ("Check a video edit", "video", ["Super", "Shift"], "v"),
          ("Pause / resume a walkthrough", "pause-toggle", ["Super"], "p")]


# ---- checks
def needs_setup():
    from ..providers import setup_ready
    return not setup_ready()


def pretty_accel(accel):
    """"<Super><Shift>space" -> "Super+Shift+Space"."""
    mods = re.findall(r"<([^>]+)>", accel)
    key = re.sub(r"<[^>]+>", "", accel)
    return "+".join(mods + ([key[:1].upper() + key[1:]] if key else []))


def accel_tokens(accel):
    """"<Super><Shift>space" -> the keycaps ["Super", "Shift", "Space"]."""
    return pretty_accel(accel).split("+") if accel else []


def flippy_shortcuts(path=None):
    """[(gtk accelerator, flippy-ask args)] from COSMIC's custom shortcuts file."""
    try:
        with open(path or SHORTCUTS) as f:
            text = f.read()
    except OSError:
        return []
    out = []
    for m in re.finditer(r'\(\s*modifiers:\s*\[([^\]]*)\],(?:\s*key:\s*"([^"]+)",)?\s*\):\s*Spawn\("([^"]*flippy-ask[^"]*)"\)', text):
        mods = [x.strip() for x in m.group(1).split(",") if x.strip()]
        accel = "".join(f"<{'Control' if x == 'Ctrl' else x}>" for x in mods) + (m.group(2) or "")
        args = m.group(3).split("flippy-ask", 1)[1].strip()
        out.append((accel, args))
    return out


def missing(bound=None):
    """The WANTED indexes COSMIC has no shortcut for."""
    bound = {args for _, args in (flippy_shortcuts() if bound is None else bound)}
    return [i for i, w in enumerate(WANTED) if w[1] not in bound]


def missing_text(which):
    """"Not in COSMIC yet: circle something, then ask (Super+Alt)." for the WANTED indexes that aren't bound."""
    def keys(mods, key):
        return "+".join(mods + ([key.title()] if key else []))
    return "Not in COSMIC yet: " + ", ".join(
        f"{WANTED[i][0][0].lower()}{WANTED[i][0][1:]} ({keys(WANTED[i][2], WANTED[i][3])})" for i in which) + "."


def state():
    """Everything the Setup page shows, as plain data: sign-ins, open at login and COSMIC's shortcuts, which COSMIC
    runs (Flippy's own key settings don't apply here). No screen or accessibility keys: those are macOS's
    permissions, and flippy/shell/setup.py page_state counts a missing "screen" as granted."""
    bound = flippy_shortcuts()
    names = {w[1]: w[0] for w in WANTED}
    gone = missing(bound)
    return {
        "claude": logged_in("claude"), "codex": logged_in("codex"),
        "login_item": desktop.autostart_on(), "login_item_allowed": not current().demo,
        "act_note": ("For desktop tasks, Flippy turns on Linux's accessibility layer (apps already open may need a "
                     "restart to show their controls). The first time it clicks by position, COSMIC asks once to allow "
                     "remote desktop. Both need COSMIC Epoch 1.7 or newer."),
        "shortcuts": [{"label": names.get(args, f"flippy-ask {args}".strip()), "keys": accel_tokens(accel),
                       "command": f"flippy-ask {args}".strip()} for accel, args in bound],
        "shortcuts_missing": [WANTED[i][0] for i in gone],
        "shortcuts_note": missing_text(gone) if gone else None,
    }


# ---- actions
class NoTerminal(ValueError):
    """No terminal app to run it in. args: (the command for them to run themselves, what to do after). A
    ValueError, so the page shows it as the action's error."""
    def __str__(self):
        command, after = self.args
        return f"No terminal app found. Open a terminal and run: {command}" + (f". {after}" if after else "")


def terminal_argv(cmd, which=shutil.which):
    """The argv that runs the shell command `cmd` in the first installed terminal, or None."""
    term = next((t for t in TERMINALS if which(t[0])), None)
    return term + ["sh", "-c", cmd] if term else None


def _in_terminal(cmd, shown, after=""):
    argv = terminal_argv(cmd)
    if argv is None:
        raise NoTerminal(shown, after)
    subprocess.Popen(argv, start_new_session=True, env=terminal_env())
    return True


def open_login_terminal():
    """Run `claude` in a terminal so they can /login with their Pro/Max account."""
    cli = shlex.quote(claude_cli())
    return _in_terminal(f"unset ANTHROPIC_API_KEY; {cli}; exec $SHELL", f"env -u ANTHROPIC_API_KEY {cli}",
                        "Then type /login and sign in with your Pro/Max account.")


def open_codex_login():
    from ..codex_provider import codex_executable, login_command
    cli = codex_executable()  # not only PATH: apps get a minimal one
    if not cli:
        subprocess.Popen(["xdg-open", "https://developers.openai.com/codex/cli/"])
        return False
    # login_command puts node on PATH too (an npm-installed Codex is a node script, and an app's PATH often hasn't
    # got it). Keep the window open if signing in fails, to read why.
    cmd = (login_command(cli)
           + " || { echo; echo 'Signing in to Codex failed (see above). Press Enter to close.'; read _; }")
    return _in_terminal(cmd, login_command(cli))


def _entry(mods, key, args):
    lines = ["    (\n", "        modifiers: [\n"]
    lines += [f"            {m},\n" for m in mods]
    lines.append("        ],\n")
    if key:
        lines.append(f'        key: "{key}",\n')
    lines.append(f'    ): Spawn("{(ASK + " " + args).strip()}"),\n')
    return "".join(lines)


def add_shortcuts(which, path=None):
    """Append the WANTED entries at `which` (indexes) to COSMIC's custom shortcuts, keeping a backup."""
    path = path or SHORTCUTS
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        text = "{\n}"
    body = text.rstrip()
    if not body.endswith("}"):
        raise ValueError(f"don't understand {path}; add the shortcuts in COSMIC Settings instead")
    body = body[:-1].rstrip()
    if not body.endswith(("{", ",")):
        body += ","
    new = body + "\n" + "".join(_entry(WANTED[i][2], WANTED[i][3], WANTED[i][1]) for i in which) + "}\n"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak-flippy")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(new)
    os.replace(tmp, path)


def add_missing_shortcuts():
    gone = missing()
    if gone:
        try:
            add_shortcuts(gone)
        except OSError as e:
            raise ValueError(f"Couldn't add the shortcuts: {e}") from e


def open_keyboard_settings():
    """COSMIC Settings, Keyboard: where the shortcuts' keys are changed."""
    if not shutil.which("cosmic-settings"):
        raise ValueError("Change shortcuts in your desktop's keyboard settings.")
    subprocess.Popen(["cosmic-settings", "keyboard"], start_new_session=True)


ACTIONS = {
    "claude": open_login_terminal, "codex": open_codex_login,
    "shortcuts": add_missing_shortcuts, "keyboard": open_keyboard_settings, **login_items(desktop.set_autostart),
}


def act(name):
    run(ACTIONS, name)
