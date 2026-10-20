#!/usr/bin/env bash
# Install Flippy on Linux (COSMIC/Wayland, Pop!_OS 24.04): the README's steps, automated.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LS_VERSION=v1.3.0
LS_SRC="$HOME/.local/src/gtk4-layer-shell"
BIN_DIR="$HOME/.local/bin"

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

# 1. System packages.
say "Installing system packages (sudo)"
sudo apt-get install -y git python3-venv python3-gi python3-gi-cairo python3-cairo python3-dbus python3-pil \
    gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-atspi-2.0 gir1.2-webkit-6.0 at-spi2-core \
    meson ninja-build libgtk-4-dev libwayland-dev gobject-introspection libgirepository1.0-dev

# 2. gtk4-layer-shell (not packaged for Ubuntu/Pop!_OS 24.04), into ~/.local. Meson puts it in lib/<arch>/ on
# Debian/Ubuntu (x86_64 or aarch64), lib/ or lib64/ elsewhere; bin/flippy-daemon looks in the same places.
have_layer_shell() {
    local so
    for so in "$HOME"/.local/lib/*/libgtk4-layer-shell.so "$HOME"/.local/lib/libgtk4-layer-shell.so \
              "$HOME"/.local/lib64/libgtk4-layer-shell.so; do
        [ -e "$so" ] && return 0
    done
    return 1
}
if ! have_layer_shell; then
    say "Building gtk4-layer-shell $LS_VERSION"
    [ -d "$LS_SRC" ] || git clone --branch "$LS_VERSION" https://github.com/wmww/gtk4-layer-shell "$LS_SRC"
    (cd "$LS_SRC" && meson setup build --prefix "$HOME/.local" -Dexamples=false -Ddocs=false -Dtests=false -Dvapi=false \
        && ninja -C build install)
fi

# 3. Python environment (system site-packages for PyGObject/GTK).
[ -x "$ROOT/.venv/bin/python" ] || python3 -m venv --system-site-packages "$ROOT/.venv"
say "Installing Python packages"
"$ROOT/.venv/bin/pip" install -q --upgrade -r "$ROOT/requirements-linux.txt"

# 4. Commands on PATH.
mkdir -p "$BIN_DIR"
ln -sf "$ROOT/bin/flippy-ask" "$BIN_DIR/flippy-ask"
ln -sf "$ROOT/bin/flippy-daemon" "$BIN_DIR/flippy-daemon"

# 5. Flippy in the app library, with its icon (flippy/linux/desktop.py).
say "Adding Flippy to the app library"
(cd "$ROOT" && "$ROOT/.venv/bin/python" -m flippy.linux.desktop install)

cat <<MSG

Done. Two things left (or run \`$BIN_DIR/flippy-ask setup\`, which does both for you):
  1. Log in to Claude if you haven't: run \`claude\` once and /login with your Pro/Max account.
  2. Add the hotkeys in COSMIC Settings -> Keyboard -> Keyboard shortcuts -> Custom shortcuts:
       Super+Shift+Space   $BIN_DIR/flippy-ask
       Super+Alt           $BIN_DIR/flippy-ask draw
       Super+P (optional)  $BIN_DIR/flippy-ask pause-toggle
       Super+Shift+V (opt.) $BIN_DIR/flippy-ask video
Flippy's icon shows up in the panel once it's running (help mode, tips, settings, updates).
MSG
