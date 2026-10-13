# Catching COSMIC up with main (October 2026): instructions for the Linux session

Run this on the Pop!_OS COSMIC machine, in a Claude Code session in the Flippy checkout. Since 0.3.0, `main` got
fixes and features for both platforms. They were written and tested on macOS, and the Linux side was checked with
the test suite on Ubuntu 24.04 (x86_64 in CI, aarch64 in Docker), **but none of it has run on a real COSMIC
desktop yet**. This file says what changed, how to update, what to check by hand, and the few things Linux still
lacks.

No version bump yet: `VERSION` stays 0.3.0 until Kapil releases from macOS. Don't release from Linux.

## Rules

- **macOS must keep working exactly as it does.** Linux fixes go in `flippy/linux/`. Touch a shared file
  (`flippy/daemon.py`, `flippy/actions.py`, `flippy/shortcuts.py`, `flippy/onboarding.py`...) only when the controller
  needs a hook, keep it platform-neutral, and run the full test suite. Mac-only tests skip here; that's fine.
- **Never test on the user's real data.** Use windows you open yourself (a new COSMIC Text Editor document, a new
  Firefox window on about:blank) and close them without saving.
- **Ask before anything that shows the user's screen to Claude** (an `/act` run, a normal ask). Unit tests and
  `flippy-ask demo-*` commands are fine.
- Commit in small pieces, each passing the tests. End commit messages with the `Co-Authored-By` line.

## 1. Update

```bash
cd ~/flippy            # or wherever the checkout is (FLIPPY_DIR)
git pull --ff-only
./install.sh           # apt packages, gtk4-layer-shell, the Python environment, the app library entry
flippy-ask quit; ~/.local/bin/flippy-ask   # restart the daemon on the new code
.venv/bin/python -m unittest discover -s tests   # everything passes; the macOS-only tests skip
```

If the daemon doesn't start, run `~/.local/bin/flippy-daemon` in a terminal and read the error (section 3.1).

## 2. What changed (all on main)

### Both platforms

| What | Where | Notes |
|---|---|---|
| **Ask Flippy to do things, no `/act` needed.** "open Spotify and play some jazz" runs as a desktop task; "how do I…" is still answered and pointed. The tutor replies `[ACT]` alone for a request to do something; the daemon holds the start of each reply until it can't be `[ACT]`, then hands the same words to the task runner. | `flippy/brain.py` (SYSTEM_PROMPT "doing things"), `flippy/daemon.py` (`_can_infer_act`, `_stream_text`, `_on_answer`, `ACT_TAG`) | Setting `act.infer` (default on), Settings → Desktop tasks → **Do things when I ask**. Linux qualifies because its ui has `app_look`. Never for a question about a drawn mark or while a video records. |
| **Act automatically covers browsers.** Terminals, settings, password managers and system prompts still ask in every mode. | `flippy/actions.py` (`ApprovalPolicy.browser`, `always_asks`, `sensitive`, `needs_approval`) | Linux browser ids/words are in `BROWSERS` / `BROWSER_WORDS`; the Linux always-ask list is `LINUX_SENSITIVE` / `LINUX_WORDS` minus browsers. |
| **No approval card for switching apps** (`use_app`), except in Ask before every input. | `daemon._action_approve` | A task started from a terminal used to ask before even opening Spotify. |
| **Approval cards in words**: "switch to Spotify / Play a song", exact escaped text only for typing and app actions. | `actions.AppFrame.describe` | Shared: `daemon._app_look` wraps `atspi.look()`'s result in `AppFrame`, so COSMIC gets it. |
| **↑/↓ history in the question box** (#8): last 50 prompts, in memory only, consecutive duplicates once, the half-typed draft comes back. | `flippy/history.py`; `linux/ui.py` `InputBox.submit/recall/_on_key` | The box's hint now reads "Enter to ask or get it done · ↑ earlier · …". |
| **Shortcuts that won't work, in the tour and the menu** (#5). The tour's new second step lists them with **Change…** and moves on by itself once fixed (skipped if all work); later steps whose shortcut is broken offer Change… instead of waiting forever; the panel menu marks them with ⚠. | `flippy/shortcuts.py` (`cosmic_problems`, `cosmic_bindings`), `flippy/onboarding.py`, `linux/ui.py` `shortcut_problems/change_shortcuts`, `linux/tray.py` `shortcuts()` | COSMIC problems: an action with no shortcut, or keys that COSMIC's own shortcuts (`/usr/share/cosmic/…/defaults`, `~/.config/cosmic/…/system_actions`) or another custom shortcut also use. **Change…** opens Flippy's setup. |
| Changing model, effort or provider stops only the session start, not tips or login checks (#9). | `daemon._run(provider_bound=…)`, `_on_setting` | |
| Codex failures log the failing request, its error code and the exception type. | `codex_provider._log_rpc_error` | Look in `~/.local/state/flippy.log` after a CODEX-FAILED. |
| Update checks skip `git` outside a checkout; the old-app update message points to the install command. | `flippy/updates.py` | |
| `script/` folder removed (#13); everything is in `scripts/`. | | |

### Linux only

| What | Where |
|---|---|
| The release tarball can install again: `make_icon.py` moved to `packaging/` (it was left out with `packaging/macos`). | `packaging/make_icon.py`, `flippy/linux/desktop.py`, `scripts/package.sh` |
| aarch64: `bin/flippy-daemon` finds gtk4-layer-shell in `~/.local/lib/<arch>/`, `lib/` or `lib64/` (FLIPPY_LAYER_SHELL_LIB first). | `bin/flippy-daemon`, `scripts/install_linux.sh` |
| Children don't inherit the gtk4-layer-shell `LD_PRELOAD` (apps `/act` opens, terminals, xdg-open, the Claude CLI). | `flippy/linux/preload.py`, called from `linux/ui.run()` |
| Logins open in `cosmic-term` first; with no terminal, setup shows the command to run. | `flippy/linux/setup_window.py` |
| `use_app` reuses an app that's already open under another name ("Text Editor" for COSMIC Text Editor) instead of launching it again and timing out. | `flippy/linux/atspi.py` `open_app`, `entry_ids`, `is_app`, `running_window` |
| Restart quotes the code path (a path with `'` broke restart and update). | `linux/ui.py` `restart` |
| Setup says what `/act` needs: AT-SPI (apps open before may need a restart), the RemoteDesktop prompt, Epoch 1.7+. | `flippy/linux/setup_window.py` |

### macOS only (for reference; nothing to do here)

Starting `/act` from the last app in front when Flippy is, asking for Accessibility, loose app names, real key
codes for punctuation, Apple Events usage text, ⌘A/C/X/V/Z in Flippy's text fields (`mac/ui.py`
`install_edit_keys`; GTK entries already have Ctrl+A/C/X/V/Z), no git dialog, the Launcher's legacy folder.

## 3. Check by hand on COSMIC

Write down what happens for each; fix what fails (section 4 has the known gaps).

1. **The daemon starts** (this exercises the aarch64 lookup and the preload scrub). `cat /proc/$(pgrep -f flippy.daemon)/environ | tr '\0' '\n' | grep LD_PRELOAD` shows nothing (the scrub ran after GTK loaded).
2. **Ask box keys.** Open it, type, then: Ctrl+A, Ctrl+C, Ctrl+X, Ctrl+V, Ctrl+Z, Ctrl+Shift+Z, Ctrl+←/→, Ctrl+Backspace, Home/End all edit as usual; ↑ brings back the last question, ↑ again the one before, ↓ back to what you'd typed; Return sends; Esc closes.
3. **Do things without /act**, in a window you opened: "open Text Editor and type hello" starts a task (approval card first in Ask once per app); "how do I make text bold in Text Editor" gets an answer and pointing. **Ask the user first** (it sends a screenshot).
4. **Approval cards** read "switch to Text Editor / <reason>", no JSON. In **Act automatically**, a task in Firefox runs without cards; a task in COSMIC Terminal still asks for every input.
5. **use_app** with Text Editor already open: it reuses the window, no second launch, no 10 s wait.
6. **Shortcut warnings.** Bind `Super+Alt` to something else in COSMIC Settings → Keyboard → Custom shortcuts as well as to `flippy-ask draw`: the panel menu shows ⚠ on Circle, and `flippy-ask tour` (then Let's go) shows "A shortcut won't work" with the reason. Remove the extra binding: within a couple of seconds the tour says "All set" and moves on. Also check **no false ⚠** with the normal setup: the regex in `shortcuts.BINDING_RE` was written against the custom file's layout and a guess at COSMIC's defaults file (`/usr/share/cosmic/com.system76.CosmicSettings.Shortcuts/v1/defaults`); open that file, and if its entries look different, fix the regex and add a sample to `tests/test_shortcuts.py`.
7. **Logins.** Setup's Claude and Codex buttons open `cosmic-term`. The argument form `cosmic-term -e sh -c <cmd>` hasn't been tried on a real machine; if it fails, check `cosmic-term --help` and fix `setup_window.py`.
8. **Restart and update** from the panel menu work (`flippy-ask update` when a release is out).

## 4. Known gaps on Linux (small; do them after section 3)

1. **Start `/act` when Flippy itself is focused and no other window ever was.** macOS falls back to Finder, from where `use_app` opens any app (`mac/ax.py` `front_app`). COSMIC's `atspi.before_flippy` raises instead. Fall back to the file manager (`com.system76.CosmicFiles`) or start with no target and require `use_app` first. Add a test next to `tests/test_linux_act.py`.
2. ~~**Terminals Flippy opens inherit `FLIPPY_*` variables.**~~ Done: `linux/setup.py` `terminal_env`, like macOS.
3. **Settings → Shortcuts and setup's shortcut rows don't show ⚠.** The panel menu and the tour do. Add the reason under a broken row using `Platform.shortcut_problems()` (keep it one muted line, like the rest of that page).

When done, open a PR to `main` (don't push to main directly from Linux), and say in it which of section 3 passed.
