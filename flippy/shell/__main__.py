"""Open the app window on its own, without the daemon: `python -m flippy.shell [route]`.

For working on the window and showing it. It reads and writes the real settings unless XDG_CONFIG_HOME points
somewhere else, and "Preview on screen" needs the daemon, so it only says so here.
"""
import sys

from .. import settings


def _no_daemon(cmd):
    raise ValueError("Preview on screen works when Flippy is running.")


def main():
    settings.load()
    route = sys.argv[1] if len(sys.argv) > 1 else "settings/appearance"
    if sys.platform == "darwin":
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        from PyObjCTools import AppHelper
        from ..mac.shell_window import AppWindow
        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        window = AppWindow(on_close=AppHelper.stopEventLoop, command=_no_daemon, log=lambda m: None)
        window.present(route)
        AppHelper.runEventLoop()
    else:
        from ..linux.shell_window import run_standalone  # flippy/linux/shell_window.py
        run_standalone(route, _no_daemon)


if __name__ == "__main__":
    main()
