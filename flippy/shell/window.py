"""What the macOS and Linux app windows share (flippy/mac/shell_window.py, flippy/linux/shell_window.py): the
bridge, the route, JSON both ways; and AppHost, the Platform side that opens the one window (flippy/mac/ui.py,
flippy/linux/ui.py)."""
import json

from .bridge import MAX_MESSAGE, Bridge


class Window:
    """A platform's AppWindow builds its window and web view (self.web) and implements _show(), _eval(script),
    set_chrome(mode) and close()."""
    web = None

    def __init__(self, host, command, log, caps=dict):
        self.log = log
        self.caps = caps  # caps(): what this desktop can do (bridge.Host.capabilities)
        self.bridge = Bridge(push=self._push, command=command, log=log, host=host)

    @property
    def route(self):
        """The page showing now ("setup", "settings/appearance"...), or the one it's opening on."""
        return self.bridge.current or self.bridge.route

    def present(self, route=None):
        if route:
            self.bridge.route = route    # read by the page when it loads
            self.bridge.current = route
            self._push("route", route)   # and sent, in case it already has
        self._show()

    def _message(self, body):
        try:
            message = json.loads(body) if isinstance(body, str) and len(body) <= MAX_MESSAGE + 4096 else None
        except ValueError:
            message = None
        self._js("window.flippy.reply", self.bridge.handle(message))

    def _push(self, event, data):
        self._js("window.flippy.event", event, data)

    def _js(self, fn, *args):
        if self.web is not None:
            self._eval(f"{fn}({', '.join(json.dumps(a) for a in args)})")


class AppHost:
    """The app window as the Platform keeps it: one, built when first asked for, dropped when it closes. The
    Platform implements _new_app_window() (its AppWindow, closing through _app_closed)."""
    app_window = None

    def open_app(self, route):
        """The app window (history, setup, settings) on route: "setup", "history", "settings/<page>"."""
        if self.app_window is None:
            self.app_window = self._new_app_window()
        self.app_window.present(route)

    def _app_closed(self):
        self.app_window = None

    @property
    def setup_win(self):
        """The app window while it shows Setup (the daemon polls the login only then)."""
        w = self.app_window
        return w if w is not None and w.route == "setup" else None

    @property
    def settings_win(self):
        w = self.app_window
        return w if w is not None and (w.route or "").startswith("settings") else None

    def open_setup(self):
        self.open_app("setup")

    def open_settings(self):
        """Settings in the app window. Its Preview goes through the daemon's own command."""
        self.open_app("settings/general")

    def demo_pointer(self, name, on_saved):
        """Scripted demo: draw a pointer in the app window's studio and save it."""
        self.open_app("settings/appearance")
        self.app_window.bridge.start_pointer_demo(name, on_saved)
