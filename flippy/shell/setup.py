"""The Setup page's words and its mark, worked out without a window, and the helpers both platforms' setup
modules share (flippy/mac/setup.py, flippy/linux/setup.py supply the checks and actions)."""
import base64
import io
import math
import os

import cairo

from .. import settings, themes
from ..profile import current


def data_url(png):
    """PNG bytes as a data: URL the page can show."""
    return "data:image/png;base64," + base64.b64encode(png).decode()


# ---- for the platforms' setup modules
def logged_in(name):
    """Whether "claude" or "codex" is signed in, as the providers last checked."""
    from ..providers import CONNECTION_STATUS
    return CONNECTION_STATUS[name] is True


def claude_cli():
    """The CLI the Agent SDK uses: its bundled copy, else `claude` on PATH."""
    import claude_agent_sdk
    bundled = os.path.join(os.path.dirname(claude_agent_sdk.__file__), "_bundled", "claude")
    return bundled if os.path.exists(bundled) else "claude"


def terminal_env():
    """The environment for a terminal Flippy opens: without Flippy's own variables, which every shell in it would
    inherit (FLIPPY_BUNDLED made later Flippy commands act like the app; FLIPPY_PROFILE, the wrong profile)."""
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("FLIPPY_", "PYTHON")) and k not in ("SSL_CERT_FILE", "ANTHROPIC_API_KEY")}


def login_items(set_login_item):
    """The login_on / login_off actions; the development profile never installs or removes login items."""
    def toggle(on):
        if not current().demo:
            set_login_item(on)
    return {"login_on": lambda: toggle(True), "login_off": lambda: toggle(False)}


def run(actions, name):
    """Run setup step `name` from a platform's actions; the page can only name steps that are there."""
    if name not in actions:
        raise ValueError(f"unknown setup step {name}")
    actions[name]()


def provider_note(mode, claude, codex):
    """With both connected and Use on Automatic, nothing is picked and setup can't finish: ask for one.
    Returns (note, needs attention)."""
    if mode == "auto" and claude and codex:
        return "Both are connected. Pick Claude or ChatGPT under Use to finish setup.", True
    if mode != "auto" and not {"claude": claude, "codex": codex}[mode]:
        return ("Claude" if mode == "claude" else "ChatGPT") + " isn't connected yet. Connect it above, or pick the other.", True
    return "You can switch any time in Settings, Models.", False


def ready(mode, claude, codex):
    if mode == "auto":
        return (claude + codex) == 1
    return {"claude": claude, "codex": codex}[mode]


def mark(size=64):
    """Flippy's pointing hand with its click lines (flippy/themes.py HAND), white on transparent, as a PNG URL."""
    scale = 2
    rows = themes.HAND
    px = int(size * scale / 1.5 / len(rows))
    w, h = len(rows[0]) * px, len(rows) * px
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(size * scale), int(size * scale))
    cr = cairo.Context(surface)
    ox, oy = round((size * scale - w) / 2), round(size * scale - h - (size * scale - 1.38 * h) / 2)
    cr.set_antialias(cairo.ANTIALIAS_NONE)
    for r, row in enumerate(rows):
        for c, ch in enumerate(row):
            if ch != " ":
                cr.set_source_rgb(*((0.5, 0.5, 0.52) if ch == "#" else (0.94, 0.945, 0.95)))
                cr.rectangle(ox + c * px, oy + r * px, px, px)
                cr.fill()
    cr.set_antialias(cairo.ANTIALIAS_DEFAULT)
    tip_x, tip_y = ox + (themes.HAND_TIP_COL + 0.5) * px, oy
    start, length = 0.15 * h, 0.19 * h
    cr.set_source_rgb(0.62, 0.64, 0.68)
    cr.set_line_width(0.075 * h)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    for a in (-90, -130, -50, -165, -15):
        rad = math.radians(a)
        cr.move_to(tip_x + start * math.cos(rad), tip_y + start * math.sin(rad))
        cr.line_to(tip_x + (start + length) * math.cos(rad), tip_y + (start + length) * math.sin(rad))
        cr.stroke()
    buf = io.BytesIO()
    surface.write_to_png(buf)
    return data_url(buf.getvalue())


def logos():
    """The Claude and ChatGPT logos the setup windows always used (flippy/mac/assets, flippy/linux/assets)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = {}
    for key, name in (("claude", "claude.png"), ("codex", "openai.png")):
        for folder in ("mac", "linux"):
            path = os.path.join(root, folder, "assets", name)
            if os.path.exists(path):
                with open(path, "rb") as f:
                    out[key] = data_url(f.read())
                break
    return out


def page_state(platform_state):
    """The platform's checks plus what follows from them."""
    s = dict(platform_state)
    mode = settings.get("provider", "mode")
    s["provider"] = mode
    s["note"], s["attention"] = provider_note(mode, s.get("claude", False), s.get("codex", False))
    s["connected"] = bool(ready(mode, s.get("claude", False), s.get("codex", False)))
    s["done"] = s["connected"] and s.get("screen", True)
    s["ask"] = settings.get("keys", "ask")
    return s
