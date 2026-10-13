"""User settings: ~/.config/flippy/config.toml, with defaults for anything missing.

Everything adjustable lives here. The settings window edits it live and the
daemon reacts through on_change listeners; hand-editing the file works too
(it's read at startup). A broken file falls back to defaults instead of crashing.
"""
import copy
import os
import math
import re
import tempfile
from dataclasses import dataclass
import tomllib
from .profile import current

PATH = os.path.join(current().config_dir, "config.toml")

DEFAULTS = {
    "provider": {"mode": "auto"},  # prefer an available subscription connection
    "codex": {"model": "default", "effort": "medium"},
    "act": {
        "mode": "per_app",       # per_app | auto | every_input
        "allowed_apps": [],       # remembered macOS bundle IDs, approved from an action task
        "infer": True,            # "open Spotify and play jazz" runs as a desktop task without typing /act
    },
    "claude": {
        "model": "default",     # default | opus | sonnet | haiku
        "effort": "low",        # low | medium | high | max
        "image": 1920,          # screenshot long edge sent to Claude: 1366 | 1920 | 0 (full)
    },
    "look": {
        "theme": "mono",        # see flippy/themes.py
        "pointer": "theme",     # theme | hand | ring | arrow | dot | glass | glasshand
        "pointer_size": 1.0,    # multiplier
        "text_size": 15,        # px, answer text
        "card_opacity": 0.94,
        "glass_tint": 0.5,      # 0-1: how strongly Liquid Glass (Glass and Media Player themes, the ask box) is tinted
        "glass_color": "smoke", # smoke | blue | purple | pink | red | orange | green | teal: the tint's color
        "controls": "all",      # all | players (playback controls on every theme, or only Media Player/Y2K)
        "player_shine": "wmp",  # wmp | none: gloss bars on the Media Player theme's Liquid Glass (macOS), or plain
        "frosted": True,        # COSMIC: the compositor blurs behind the Glass and Media Player cards; off: painted
                                # glass, and card_opacity applies to it like to the other themes
    },
    "timing": {
        "show_seconds": 8,      # answer stays at least this long after it finishes
        "max_show_seconds": 20,
        "step_pace": "normal",  # slow | normal | fast (how long each walkthrough step holds)
        "glide_seconds": 0.6,   # hand travel time between points
        "speed": 1.0,           # walkthrough playback speed, 0.5-2.0 (the player skins' speed slider)
        "wait_for_clicks": True,  # tutorial steps marked :click wait until you click the thing (macOS)
    },
    "help": {                   # help mode (flippy/watch.py): offer a hand when you seem stuck
        "mode": "off",          # off | quiet (offer a hand when stuck) | tips (that, plus cached tips: flippy/tips.py)
        "apps": "",             # comma-separated app ids to watch (macOS bundle ids)
        "muted": "",            # apps where you chose "don't ask"
    },
    "history": {
        "keep": True,             # save what you ask and what Flippy did (text only, never screenshots)
        "days": 30,               # then it's deleted
    },
    "window": {
        "mode": "system",       # the app window (history, setup, settings): system | light | dark
    },
    "onboarding": {
        "done": False,          # the first-run tour was finished or dismissed
    },
    "updates": {
        "check": True,          # look for a new GitHub Release once a day and offer to install it
    },
    "automation": {
        "clicks": False,        # let `flippy-ask click` click on screen (macOS: needs the Accessibility permission)
    },
    "keys": {                   # global hotkeys (macOS; on COSMIC they're set in COSMIC Settings)
        "ask": "cmd+shift+space",
        "draw": "ctrl+shift+space",
        "video": "ctrl+option+v",  # video review: start recording the window in front; again: stop and ask
        "pause": "double-cmd",  # pause/resume the walkthrough: double-cmd | double-option | double-ctrl | double-shift | off
    },
}

if current().demo:
    DEFAULTS["keys"].update(ask="cmd+option+shift+space", draw="ctrl+option+shift+space",
                            pause="double-option", video="ctrl+option+shift+v")
    DEFAULTS["updates"]["check"] = False


@dataclass(frozen=True)
class Setting:
    default: object
    choices: tuple = ()
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    digits: int = 0


_OPTIONS = {
    ('provider', 'mode'): [('auto', 'Automatic'), ('claude', 'Claude'), ('codex', 'Codex / ChatGPT')],
    ('codex', 'effort'): [('low', 'Low'), ('medium', 'Medium'), ('high', 'High'), ('xhigh', 'Extra high')],
    ('act', 'mode'): [('per_app', 'Ask once per app'), ('auto', 'Act automatically'), ('every_input', 'Ask before every input')],
    ('claude', 'model'): [('default', 'Account default'), ('opus', 'Opus'), ('sonnet', 'Sonnet'), ('haiku', 'Haiku')],
    ('claude', 'effort'): [('low', 'Low (fastest)'), ('medium', 'Medium'), ('high', 'High'), ('max', 'Max (slowest)')],
    ('claude', 'image'): [(1366, '1366 px (lightest on usage)'), (1920, '1920 px (balanced)'), (0, 'Full resolution (sharpest)')],
    ('look', 'pointer'): [('theme', 'Theme default'), ('hand', 'Pixel hand'), ('arrow', 'Pixel arrow'), ('ring', 'Ring'), ('dot', 'Dot'), ('glass', 'Liquid glass lens'), ('glasshand', 'Liquid glass hand')],
    ('timing', 'step_pace'): [('slow', 'Slow'), ('normal', 'Normal'), ('fast', 'Fast')],
    ('look', 'controls'): [('all', 'On every theme'), ('players', 'Only Media Player and Y2K')],
    ('look', 'player_shine'): [('wmp', 'WMP gloss'), ('none', 'Plain glass')],
    ('look', 'glass_color'): [('smoke', 'Smoke'), ('blue', 'Blue'), ('purple', 'Purple'), ('pink', 'Pink'), ('red', 'Red'), ('orange', 'Orange'), ('green', 'Green'), ('teal', 'Teal')],
    ('keys', 'pause'): [('double-cmd', '⌘ ⌘  (double-tap)'), ('double-option', '⌥ ⌥  (double-tap)'), ('double-ctrl', '⌃ ⌃  (double-tap)'), ('double-shift', '⇧ ⇧  (double-tap)'), ('off', 'Off')],
    ('help', 'mode'): [('off', 'Off'), ('quiet', "Offer a hand when I'm stuck"), ('tips', 'That, plus tips while I work')],
    ('window', 'mode'): [('system', 'System'), ('light', 'Light'), ('dark', 'Dark')],
    ('look', 'theme'): [('mono', 'Mono'), ('midnight', 'Midnight'), ('y2k', 'Y2K'), ('mediaplayer', 'Mediaplayer'), ('glass', 'Glass'), ('terminal', 'Terminal'), ('cosmic', 'Cosmic')],
}
_BOUNDS = {
    ('look', 'pointer_size'): (0.5, 2.0, 0.1, 1),
    ('look', 'text_size'): (11, 24, 1, 0),
    ('look', 'card_opacity'): (0.5, 1.0, 0.02, 2),
    ('look', 'glass_tint'): (0.0, 1.0, 0.05, 2),
    ('timing', 'show_seconds'): (3, 60, 1, 0),
    ('timing', 'max_show_seconds'): (5, 120, 1, 0),
    ('timing', 'speed'): (0.5, 2.0, 0.05, 2),
    ('timing', 'glide_seconds'): (0.2, 2.0, 0.1, 1),
    ('history', 'days'): (1, 365, 1, 0),
}
SETTINGS = {
    (section, key): Setting(value, tuple(_OPTIONS.get((section, key), ())),
                           *_BOUNDS.get((section, key), (None, None, None, 0)))
    for section, values in DEFAULTS.items() for key, value in values.items()
}
DEFAULTS = {section: {key: SETTINGS[section, key].default for key in values}
            for section, values in DEFAULTS.items()}
CHOICES = {key: [value for value, _ in spec.choices]
           for key, spec in SETTINGS.items() if spec.choices}


def options(section, key):
    return list(SETTINGS[section, key].choices)


@dataclass(frozen=True)
class ActionLimits:
    max_actions: int
    max_text: int
    timeout_seconds: int
    max_turns: int


ACT_LIMITS = {
    "every_input": ActionLimits(12, 160, 300, 16),
    "per_app": ActionLimits(40, 2000, 600, 64),
    "auto": ActionLimits(40, 2000, 600, 64),
}


def action_limits(mode=None):
    return ACT_LIMITS[mode if mode is not None else get("act", "mode")]


def valid_bundle_id(value):
    return (type(value) is str and len(value) <= 255
            and re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", value) is not None)


def remove_allowed_app(bundle_id):
    """Settings UI revocation. Approval, never this helper, grants access."""
    set("act", "allowed_apps", [app for app in get("act", "allowed_apps") if app != bundle_id])


VERSION = 2  # the config file's format; older files are migrated on load (see _migrate)

_data = copy.deepcopy(DEFAULTS)
_listeners = []


def _migrate(user):
    """Version 1 -> 2: the glossy media player theme was "glass" and is now "mediaplayer"; the lock-screen
    style one was "nowplaying" and is now "glass". Its reflections setting moved with it."""
    if isinstance(user.get("version", 1), int) and user.get("version", 1) >= 2:
        return user
    look = user.get("look")
    if isinstance(look, dict):
        if isinstance(look.get("theme"), str):
            look["theme"] = {"glass": "mediaplayer", "nowplaying": "glass"}.get(look["theme"], look["theme"])
        if "glass_shine" in look:
            look["player_shine"] = look.pop("glass_shine")
    return user


def load():
    global _data
    data = copy.deepcopy(DEFAULTS)
    try:
        with open(PATH, "rb") as f:
            user = _migrate(tomllib.load(f))
        for section, values in user.items():
            if section in data and isinstance(values, dict):
                for k, v in values.items():
                    if k in data[section] and _valid(section, k, v):
                        data[section][k] = v
                    elif k in data[section]:
                        print(f"flippy: ignoring invalid setting {section}.{k}", flush=True)
    except FileNotFoundError:
        pass
    except (tomllib.TOMLDecodeError, OSError):
        print("flippy: ignoring unreadable or invalid settings file", flush=True)
    _data = data


def _valid(section, key, value):
    spec = SETTINGS.get((section, key))
    if spec is None:
        return False
    default = spec.default
    if (section, key) == ("act", "allowed_apps"):
        return (type(value) is list and len(value) <= 128
                and all(valid_bundle_id(app) for app in value)
                and len(dict.fromkeys(value)) == len(value))
    if (section, key) == ("codex", "model"):
        return type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) is not None
    if isinstance(default, bool):
        if type(value) is not bool:
            return False
    elif isinstance(default, (int, float)):
        if type(value) not in (int, float):
            return False
        try:
            if not math.isfinite(value):
                return False
        except OverflowError:
            return False
        if type(default) is int and type(value) is not int:
            return False
        if spec.minimum is not None and not spec.minimum <= value <= spec.maximum:
            return False
    elif type(value) is not type(default):
        return False
    if (section, key) == ("look", "pointer") and value.startswith("custom:"):
        from . import pointers
        return pointers.exists(value[len("custom:"):])
    return not spec.choices or value in CHOICES[section, key]


def parse_value(key, raw):
    """Parse a CLI section.key using exactly the programmatic validation rules."""
    if isinstance(key, str):
        key = tuple(key.split("."))
    spec = SETTINGS.get(key)
    if spec is None:
        raise ValueError("unknown setting")
    try:
        if type(spec.default) is list:
            value = tomllib.loads("value = " + raw)["value"]
        elif isinstance(spec.default, bool):
            if raw.lower() not in ("true", "false"):
                raise ValueError("expected true or false")
            value = raw.lower() == "true"
        elif type(spec.default) is int:
            value = int(raw)
        elif type(spec.default) is float:
            value = float(raw)
        else:
            value = raw
    except (ValueError, TypeError, AttributeError):
        raise ValueError("invalid setting value") from None
    if not _valid(*key, value):
        raise ValueError("invalid setting value")
    return value


def get(section, key):
    value = _data[section][key]
    return value.copy() if isinstance(value, list) else value


def get_list(section, key):
    """A comma-separated string setting as a set."""
    value = get(section, key)
    if isinstance(value, list):
        return {v for v in value}
    return {v.strip() for v in value.split(",") if v.strip()}


def set_list(section, key, values):
    value = sorted(dict.fromkeys(values))
    set(section, key, value if isinstance(DEFAULTS[section][key], list) else ",".join(value))


def set(section, key, value):  # noqa: A001
    global _data
    if not _valid(section, key, value):
        raise ValueError(f"invalid setting {section}.{key}")
    if _data[section][key] == value:
        return
    candidate = copy.deepcopy(_data)
    candidate[section][key] = copy.deepcopy(value)
    _save(candidate)
    _data = candidate
    for fn in list(_listeners):
        fn(section, key, copy.deepcopy(value))


def on_change(fn):
    _listeners.append(fn)


def off_change(fn):
    if fn in _listeners:
        _listeners.remove(fn)


def save():
    _save(_data)


def _save(data):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    lines = ["# Flippy settings. Edit here or via `flippy-ask settings`.\n", f"version = {VERSION}\n"]
    for section, values in data.items():
        lines.append(f"\n[{section}]\n")
        for k, v in values.items():
            lines.append(f"{k} = {_toml(v)}\n")
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=os.path.dirname(PATH))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _toml(v):
    if isinstance(v, list):
        return "[" + ", ".join(_toml(item) for item in v) + "]"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v) if isinstance(v, float) else str(v)
    escapes = {"\\": "\\\\", '"': '\\"', "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}
    text = "".join(escapes.get(c, f"\\u{ord(c):04x}" if ord(c) < 32 or ord(c) == 127 else c)
                   for c in str(v))
    return '"' + text + '"'


load()
