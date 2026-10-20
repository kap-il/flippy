"""Settings pages for the app window, generated from flippy/settings.py.

Every setting shows up somewhere: a key without an entry in LABELS gets a label made from its name, and a section
without a page entry gets its own page. So settings added elsewhere appear without window work. Only the copy,
grouping and when-to-show live here; types, choices, bounds and validation stay in settings.py.
"""
import os
import sys

from .. import settings

# page id, title, icon, and its groups: (group title or None, [sections])
PAGES = [
    ("general", "General", "gear", [(None, ["provider", "updates"]), ("History", ["history"]), ("Scripting", ["automation"])]),
    ("appearance", "Appearance", "sun", [(None, ["window", "look"])]),  # laid out by the page (appearance.js)
    ("models", "Models", "sparkle", [("Claude", ["claude"]), ("Codex / ChatGPT", ["codex"])]),
    ("tasks", "Desktop tasks", "cursor", [(None, ["act"])]),
    ("shortcuts", "Shortcuts", "keyboard", [(None, ["keys"])]),
    ("timing", "Timing", "timer", [(None, ["timing"])]),
    ("help", "Help mode", "lifebuoy", [(None, ["help"])]),
]
HIDDEN = {("onboarding", "done")}  # state, not a preference
ADVANCED = {("timing", "glide_seconds"), ("timing", "speed"), ("claude", "image"), ("help", "muted")}

# (section, key): (label, help, platforms or None for all)
LABELS = {
    ("provider", "mode"): ("Use", "Automatic picks the only connected subscription.", None),
    ("updates", "check"): ("Check for updates", "Look for a new release once a day and offer to install it.", None),
    ("automation", "clicks"): ("Let scripts click and type", "flippy-ask click, type and key, for recording demos. When on, any "
                               "program running as you can make Flippy click. Separate from task approvals.", ("darwin",)),
    ("history", "keep"): ("Keep history", "Save what you ask and what Flippy did, as text. Never screenshots.", None),
    ("history", "days"): ("Keep for", "Days, then it's deleted.", None),
    ("window", "mode"): ("Mode", "This window follows the system, or stays light or dark.", None),
    ("look", "theme"): ("Theme", "How answer cards look.", None),
    ("look", "pointer"): ("Pointer", None, None),
    ("look", "pointer_size"): ("Pointer size", None, None),
    ("look", "text_size"): ("Text size", "Answer text, in points.", None),
    ("look", "card_opacity"): ("Card opacity", None, None),
    ("look", "glass_tint"): ("Tint strength", None, None),
    ("look", "glass_color"): ("Tint", None, None),
    ("look", "controls"): ("Controls on every theme", "Playback controls on every card, not only the players.", None),
    ("look", "player_shine"): ("Shine", "Gloss bars on the Media Player card.", ("darwin",)),
    ("look", "frosted"): ("Frosted glass", "Blur behind glass cards, where the compositor can.", ("linux",)),
    ("claude", "model"): ("Model", None, None),
    ("claude", "effort"): ("Effort", "Higher thinks longer and uses more of your plan.", None),
    ("claude", "image"): ("Screenshot size", "Larger is sharper and uses more of your plan.", None),
    ("codex", "model"): ("Model", "\u201cdefault\u201d uses your account\u2019s model.", None),
    ("codex", "effort"): ("Effort", None, None),
    ("act", "mode"): ("Approval", "When Flippy asks before acting in an app.", None),
    ("act", "allowed_apps"): ("Allowed apps", "Apps you approved once. Remove one to be asked again.", None),
    ("act", "infer"): ("Do things when I ask", "A request like “open Spotify” runs as a desktop task.", None),
    # COSMIC runs Flippy's shortcuts itself: the Shortcuts page shows those there (shortcuts.js)
    ("keys", "ask"): ("Ask about the screen", None, ("darwin",)),
    ("keys", "draw"): ("Circle and ask", None, ("darwin",)),
    ("keys", "video"): ("Check a video edit", None, ("darwin",)),
    ("keys", "pause"): ("Pause a walkthrough", None, ("darwin",)),
    ("timing", "show_seconds"): ("Answer stays at least", "Seconds after it finishes.", None),
    ("timing", "max_show_seconds"): ("Answer stays at most", "Seconds.", None),
    ("timing", "step_pace"): ("Step pace", "How long each walkthrough step holds.", None),
    ("timing", "glide_seconds"): ("Pointer glide", "Seconds between points.", None),
    ("timing", "speed"): ("Walkthrough speed", None, None),
    ("timing", "wait_for_clicks"): ("Wait for my clicks", "Steps marked to click wait until you click.", ("darwin",)),
    ("help", "mode"): ("Help mode", None, None),
    ("help", "apps"): ("Apps to watch", "Comma-separated app ids.", None),
    ("help", "muted"): ("Muted apps", "Apps where you chose “don't ask”.", None),
}
# Shown only while another setting has one of these values (a theme's own options stay with that theme).
WHEN = {
    ("look", "player_shine"): ("look.theme", ["mediaplayer"]),
    ("look", "frosted"): ("look.theme", ["glass", "mediaplayer"]),
}
# Shown only where the desktop can do it (Host.capabilities): hidden when the capability is False.
NEEDS = {("look", "frosted"): "blur"}
# Two-value choices shown as a switch: (value when on, value when off).
SWITCHES = {("look", "controls"): ("all", "players")}
UNITS = {("look", "text_size"): "pt", ("timing", "show_seconds"): "s", ("timing", "max_show_seconds"): "s",
         ("timing", "glide_seconds"): "s", ("history", "days"): "days", ("timing", "speed"): "×", ("look", "pointer_size"): "×"}


# Rows that do something instead of holding a setting, by page: a button that runs a bridge op, or a key to know.
def actions(plat):
    from .. import updates
    out = {"general": [
        {"group": "Session", "label": "Start a fresh session", "help": "Forget the conversation so far.", "button": "Reset",
         "op": "command", "args": {"name": "reset"}, "done": "Started a fresh session"},
        {"group": None, "label": "Check for updates now", "help": f"You have Flippy {updates.current_version()}.",
         "button": "Check", "op": "command", "args": {"name": "update"}, "done": "Checking for updates"},
        {"group": "Config file", "label": settings.PATH.replace(os.path.expanduser("~"), "~"), "help": "Edit it by hand if you like.",
         "button": "Open", "op": "open_config"}]}
    keys = [("/new", "Start a fresh session"), ("/act", "Do it for me"), ("/settings", "Open this window"), ("Esc", "Close")]
    out["shortcuts"] = [{"group": "In the question box", "label": what, "keys": key} for key, what in keys]
    return out


def platform():
    return "darwin" if sys.platform == "darwin" else "linux"


def _label_from_key(key):
    return key.replace("_", " ").capitalize()


def control(section, key):
    """The control kind for one setting, from its default and spec."""
    spec = settings.SETTINGS[section, key]
    if (section, key) in SWITCHES:
        return "switch"
    if spec.choices:
        return "choice"
    if isinstance(spec.default, bool):
        return "switch"
    if spec.minimum is not None:
        return "slider"
    if isinstance(spec.default, list):
        return "list"
    if section == "keys":
        return "shortcut"
    return "text"


def field(section, key, plat=None):
    """One setting as plain data for the page, or None when it doesn't apply on this platform."""
    label, help_text, platforms = LABELS.get((section, key), (_label_from_key(key), None, None))
    if platforms and (plat or platform()) not in platforms:
        return None
    spec = settings.SETTINGS[section, key]
    out = {"id": f"{section}.{key}", "section": section, "key": key, "label": label, "help": help_text,
           "control": control(section, key), "value": settings.get(section, key), "default": spec.default,
           "advanced": (section, key) in ADVANCED}
    if spec.choices:
        out["choices"] = [{"value": v, "label": text} for v, text in spec.choices]
    if spec.minimum is not None:
        out.update(min=spec.minimum, max=spec.maximum, step=spec.step, digits=spec.digits)
    if (section, key) in SWITCHES:
        out["on"], out["off"] = SWITCHES[section, key]
    if (section, key) in WHEN:
        out["when"] = {"id": WHEN[section, key][0], "values": WHEN[section, key][1]}
    if (section, key) in NEEDS:
        out["needs"] = NEEDS[section, key]
    if (section, key) in UNITS:
        out["unit"] = UNITS[section, key]
    return out


def pages(plat=None):
    """Every settings page with its groups of fields. Sections no page claims get a page of their own, at the end."""
    claimed = {s for *_, groups in PAGES for _, sections in groups for s in sections}
    extra = [(s, _label_from_key(s), "dots", [(None, [s])]) for s in settings.DEFAULTS if s not in claimed]
    out, more = [], actions(plat or platform())
    for page_id, title, icon, groups in PAGES + extra:
        built = []
        for group_title, sections in groups:
            fields = [f for s in sections if s in settings.DEFAULTS for k in settings.DEFAULTS[s]
                      if (s, k) not in HIDDEN and (f := field(s, k, plat)) is not None]
            if fields:
                built.append({"title": group_title, "fields": fields})
        if built or page_id == "shortcuts":  # on COSMIC it lists COSMIC's shortcuts (shortcuts.js)
            out.append({"id": page_id, "title": title, "icon": icon, "groups": built,
                        "fields": [f for g in built for f in g["fields"]], "actions": more.get(page_id, [])})
    return out
