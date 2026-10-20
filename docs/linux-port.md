# macOS features on Linux (COSMIC)

The shared code (Claude, pointing, themes, settings, help mode's rules, tips, updates,
the controller in `flippy/daemon.py`) runs on both. Each platform supplies the pieces
the controller looks for (`hasattr(self.ui, ...)`); a missing piece switches its
feature off instead of crashing, and commands that need it answer "not available on
this platform yet".

Everything COSMIC needs lives in `flippy/linux/`. Where the controller needed a hook, the shared
change is platform-neutral (a platform can bring its own `/act` tool catalog, for example), so macOS
runs what it ran before.

Flippy 0.3.0 brought COSMIC up to macOS: `/act` desktop tasks, the real pointer, real glass, Codex, and
the new look. It needs **COSMIC Epoch 1.7 or newer** for the pointer (the RemoteDesktop portal) and the
glass (`ext_background_effect_v1`); on an older COSMIC those two switch off and say so.

| Feature | On COSMIC | How |
|---|---|---|
| Help mode ("Need a hand?") | yes | [Help mode](#help-mode) |
| Tips while you work | yes | [Help mode](#help-mode) |
| Tutorials wait for your click | yes, inferred | [Tutorials](#tutorials-that-wait-for-clicks) |
| Update card | yes | [Cards](#the-cards) |
| Panel icon showing your pointer, with the menu | yes | [Panel icon](#panel-icon) |
| Esc cancels draw mode | yes | [Esc](#esc-cancels-draw-mode) |
| Settings rows (help mode, watched apps, wait for clicks, updates) | yes, in the app window | `flippy/linux/shell_window.py`, `flippy/shell/` |
| First-run setup | yes, the app window's Setup page | [Setup](#first-run-setup) |
| Flippy.app (icon, open at login) | app library entry + icon, XDG autostart | `flippy/linux/desktop.py` |
| Pause / resume shortcut | a COSMIC shortcut, not a double-tap | [Pause](#pause-shortcut) |
| Demo tools `shot`, `nudge`, `demo-nudge`, `demo-tip`, `demo-update` | yes | |
| Scripted typing (`type`, `key`, `tap`) | yes | [Typing](#scripted-typing) |
| `/act` desktop tasks | yes, through AT-SPI; keys and clicks by position borrow the window | [Desktop tasks](#desktop-tasks-act) |
| Codex / ChatGPT, the usage guard, error codes | yes | shared (`flippy/codex_provider.py`) |
| The app window (history, setup, settings, pointer studio) | yes, WebKitGTK 6 | `flippy/linux/shell_window.py` |
| Scripted mouse (`click`, `move`, `path`) | yes, through the RemoteDesktop portal (asks once) | [Pointer](#the-pointer) |
| Real Liquid Glass (Media Player, Glass, lens, glass hand), `look.glass_tint`, `look.glass_color`, `look.player_shine` | yes: the compositor's blur, Flippy's tint | [Glass](#real-glass) |
| `record` (demo screen recording) | yes | [Recording](#demo-recording) |
| Hotkeys set from Flippy's settings (`keys.ask`, `keys.draw`) | by design: COSMIC owns shortcuts | [Shortcuts](#shortcuts-by-design) |

## What Flippy can see on COSMIC

Wayland hides other apps' windows and input from ordinary clients. Flippy gets what it
needs from protocols cosmic-comp offers to every client, on a second, private Wayland
connection with its own thread (`flippy/linux/wl.py`). It speaks just enough of the
wire protocol itself, so there's no pywayland or generated bindings:

| Need | Protocol |
|---|---|
| The active window's app id, title and geometry, and new windows | `ext_foreign_toplevel_list_v1` + `zcosmic_toplevel_info_v1` (v2+: `get_cosmic_toplevel`) |
| Seconds since the last input | `ext_idle_notifier_v1` `get_input_idle_notification` (2 s timeout) |
| Where the mouse is, and how much it moves | an `ext_image_copy_capture` cursor session on the output |
| A tiny thumbnail of the active window, or the screen | `ext_image_copy_capture_v1` with a toplevel or output source, into a `wl_shm` buffer |

None of these ask for permission. Check what your cosmic-comp offers by listing the
registry's globals; anything missing turns the matching signal into `None`.

COSMIC's capture sessions offer `XBGR8888`/`ABGR8888` for shm, not the usual
`XRGB8888`, which is why `wl.SHM_FORMATS` tries them first.

## Help mode

`Platform.sample()` (`flippy/linux/sensors.py`) fills `watch.Sample` like macOS does:

| Field | COSMIC source |
|---|---|
| `app`, `app_name` | the activated toplevel's `app_id`; the name from its `.desktop` file |
| `idle_s` | idle-notify: 0 while busy, else seconds since input stopped |
| `events` | mouse moves (counted from the cursor session, never read) + 5 per idle → busy flip. Wayland can't count key presses, so the flips stand in for typing. The rules' thresholds stay as on macOS |
| `thumb` | the active **window** only, 64×40 grayscale: no panel clock, no Flippy overlay |
| `windows` | the app's toplevels with their geometry, so the dialog rule works too |

Flippy's own windows (`dev.flippy.daemon`) count as "nothing in front". `help.apps`
holds Wayland app ids (e.g. `io.lmms.LMMS`), which is what the panel menu's
**Watch <app>** adds.

## The cards

"Need a hand?", tips and the update card are drawn inside the overlay
(`flippy/linux/notice.py`: `NoticeLayer`, mixed in over the shared `OverlayBase`),
top-right under the panel. A separate layer surface isn't an option: unmapping one
drops Flippy's Wayland connection on COSMIC. The buttons join `self.hits`, so they're
the only clickable part.
`flippy/linux/ui.py` `Nudge` adds the timeouts (15 s "Not now", 25 s "Got it") and has
the same API as `flippy/mac/nudge.py`. macOS still uses its own panel.

## Tutorials that wait for clicks

Wayland never tells an app about clicks in other windows. `sensors.ClickWatcher`
infers them: while a step waits, it follows the mouse (cursor session) and compares a
128×72 screen thumbnail every 0.4 s with Flippy's own card and pointer blanked out
(`NoticeLayer.ink`). When the screen reacts (1% of it changed, another window became
active, a window opened, or the title changed), every spot the mouse was in the last
2 s is reported, and the controller only accepts one near the step's target. So
"clicked the right thing and something happened" moves on; a reaction elsewhere
doesn't.

Limits: a click that changes almost nothing on screen (a checkbox, a small toggle)
isn't noticed. **Next** on the card skips the step, as on macOS. `key_idle_s()` (for
"click the box and type X" steps) is any-input idle, not key-only.

## Panel icon

`flippy/linux/tray.py`: a StatusNotifierItem plus a `com.canonical.dbusmenu` menu over
dbus-python. The icon is drawn by `flippy/linux/statusicon.py` (a copy of the macOS
menu bar icon's drawing), white with a dark outline, re-sent when `look.pointer` or
`look.theme` changes. The menu mirrors macOS's: Ask, Circle, help mode, tips,
**Watch <app>**, **Set a goal for <app>…**, preview, settings, new session, updates,
setup, quit. The panel is a layer surface, so the active window while the menu is
open is still the app being learned. Left-click asks, middle-click circles where the
panel supports it.

## Esc cancels draw mode

The overlay can't take keys (switching a layer surface's keyboard mode back on COSMIC
leaves it eating keys). While drawing, `KeyCatcher` opens a 1×1 transparent regular
window that holds the keyboard; Esc cancels, and closing it hands the keyboard back,
just like the question box. COSMIC's active-window outline shows as a 2-px speck.

## First-run setup

`flippy-ask setup` (or the panel menu → Setup…) opens the app window on Setup
(`flippy/linux/shell_window.py`; the checks and actions are in `flippy/linux/setup.py`), the same page as macOS. Two steps, no permissions:

1. **Connect:** Claude (**Log in…** opens a terminal running `claude`) and ChatGPT through
   Codex (**Sign in…** opens a terminal running `codex login`, with node on its PATH, and
   stays open if it fails). **Use** picks which one answers.
2. **Your shortcut:** the ask shortcut as keycaps. **Add them** writes the missing ones
   into `~/.config/cosmic/com.system76.CosmicSettings.Shortcuts/v1/custom` (backed up to
   `custom.bak-flippy` first); **Edit…** opens Settings → Hotkeys, and the shortcuts
   themselves change in COSMIC Settings.

**Open Flippy at login** writes an XDG autostart entry, like macOS's LaunchAgent. Setup
opens by itself only on install, a new major version, or a lost login
(`flippy/setup_gate.py`).

## Pause shortcut

macOS's default is double-tapping ⌘ (`keys.pause`). Wayland apps can't watch keys
outside their own windows, and COSMIC shortcuts need a real key, so on COSMIC it's a
shortcut running `flippy-ask pause-toggle` (setup offers `Super+P`). For the same
reason `keys.ask` / `keys.draw` don't apply: the shortcuts live in COSMIC Settings.

## Scripted typing

`flippy-ask type/key/tap` (behind `automation.clicks`, "Let scripts type" in Settings →
Hotkeys) go through `zwp_virtual_keyboard_v1` (`flippy/linux/keyboard.py`). Like
`wtype`, each call uploads a keymap holding just the keysyms it needs, so any
character types regardless of layout. Modifiers: `ctrl`, `shift`, `alt`, `super`
(`cmd` means `super`); they go down as keys of their own, not just as modifier state,
because apps like Blender track the keys themselves.

## Demo recording

`flippy-ask record start <path>` / `record stop` (`flippy/linux/recorder.py`): the
ScreenCast portal (monitor, cursor embedded, persist mode 2 with its restore token in
`~/.config/flippy/screencast-token`) into `gst-launch-1.0 -e pipewiresrc ! … !
openh264enc ! h264parse ! mp4mux` (`qtmux` for .mov, `matroskamux` for .mkv). Stop
writes `<path>.json` with the wall clock, like macOS.

## Desktop tasks (`/act`)

`/act` works on one app, whether or not it's in front, like on macOS. `flippy/linux/atspi.py`
mirrors `flippy/mac/ax.py` through AT-SPI, Linux's accessibility layer:

- **Which app.** The window in front comes from Wayland (app id, title). A task typed in the
  question box starts in the window that had the focus before the box. AT-SPI lists apps by
  process; `match_app` pairs them by flatpak id (`/proc/<pid>/root/.flatpak-info`), the window
  title among the app's windows, or the executable its `.desktop` file names. An app that isn't on
  AT-SPI still gets screenshots, with a handle above any pid.
- **Seeing it.** The window is captured on its own (`wl.capture_window`). Controls come from the
  AT-SPI tree (only what's showing). Their positions are in WINDOW coordinates, which GTK3 counts
  from the surface (shadow included) and GTK4 from the visible window; the union of the window's
  top-level children is the visible window, so its corner maps them onto the capture.
- **Acting in the background:** press (the Action interface), set_text, focus, typing (inserted
  at the cursor through EditableText, length in bytes), and return / delete / ctrl+a/c/x/v in a
  text control. Menus come from the menu bar, or from a menu button's popover (GTK4 apps have
  no menu bar): `["Menu", "Preferences"]` presses the button, then the item.
- **Borrowing the window** (`flippy/linux/borrow.py`): Wayland can't send a key or a click to one
  app. Other keys, and clicks by position that don't land on a control, wait until the user has
  been idle for a second, bring the window forward (`zcosmic_toplevel_manager_v1.activate`), do
  the input, and give the focus back to the window they were in. The pointer can't be put back:
  Wayland doesn't let a client read where it was.
- **Opening apps:** a desktop entry by name, generic name or id; a new window takes the focus on
  COSMIC, so it's handed straight back.
- **Media and app actions:** MPRIS over D-Bus (`flippy/linux/mpris.py`), and
  `flippy/linux/scripts.py` with macOS's action names where Linux can do them (Spotify through
  MPRIS and `spotify:` links, `browser.open_url` to the default browser).
- **The tools** (`flippy/linux/act.py`): `ctrl` shortcuts, any ctrl / shift / alt combination
  except super and ctrl+alt (COSMIC handles those itself), no `real_pointer` choice.
- **Approval:** Wayland app ids can be bare names (`firefox`); terminals, settings, browsers,
  password managers and polkit prompts always ask (`ApprovalPolicy.LINUX_SENSITIVE`).

Flippy turns on `org.a11y.Status.IsEnabled`, the standard switch, but never
`ScreenReaderEnabled`. Limits: COSMIC's own apps (libcosmic) show AT-SPI little, so they're
worked by position and keys. Apps that were running before the switch flipped (Firefox,
Chromium, Electron) expose their controls only after a restart.

## The pointer

`flippy/linux/remote.py`: the RemoteDesktop portal (Epoch 1.7). The session asks once (COSMIC's
dialog); its restore token is kept in `~/.config/flippy/remote-desktop-token` with 0600
permissions, so later sessions start without asking. Absolute positions need a screen-cast stream
on the same session, which COSMIC shows in the panel, so a session closes after 20 s unused. Its
portal requests wait on a private main context, so it runs on the task's worker thread. It drives
`/act`'s clicks, scrolls and drags, and `flippy-ask click/move/path` (behind `automation.clicks`).

## Real glass

`flippy/linux/blur.py`: `ext_background_effect_v1` (Epoch 1.7). The compositor blurs what's behind
the overlay under the Glass and Media Player cards, the glass lens and the glass hand; the overlay
paints the tint over it (the user's Glass tint strength and color, `flippy/glass.py`, the same
formulas as macOS). The request has to be on GTK's own Wayland connection (the overlay's surface
belongs to it), so it goes through ctypes to libwayland-client, with a private queue for its one
roundtrip. A region is rectangles, so the rounded shapes become thin strips. Without the protocol
the themes keep their painted glass, and Settings hides the glass rows.

## Shortcuts: by design

COSMIC has no GlobalShortcuts portal, so Flippy writes its shortcuts into COSMIC's custom
shortcuts (setup offers that) instead of grabbing keys, and pause is an ordinary shortcut: Wayland
apps can't watch for a double-tap outside their own windows.
