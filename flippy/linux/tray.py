"""COSMIC's panel icon: a StatusNotifierItem with a com.canonical.dbusmenu menu, over dbus-python.

The icon is the equipped pointer with click rays (flippy/linux/statusicon.py), white
with a dark outline. The menu mirrors the macOS menu bar (flippy/mac/ui.py MENU);
items run controller commands. The panel is a layer surface, so opening the menu
doesn't change the active window: "Watch <app>" is the app they were working in.
"""
import os

import dbus
import dbus.service

from .. import settings
from . import sensors, statusicon

SNI_IFACE = "org.kde.StatusNotifierItem"
MENU_IFACE = "com.canonical.dbusmenu"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
ITEM_PATH, MENU_PATH = "/StatusNotifierItem", "/MenuBar"
ICON_PX = (22, 44)

MENU = [("Ask about the screen", "ask"), ("Circle and ask", "draw"), ("Check a video edit", "video"), None,
        ("Help when I'm stuck", "help-toggle"), ("Tips while I work", "tips-toggle"),
        ("Watch this app", "watch-front"), ("Set a goal…", "goal-front"), None,
        ("Preview the look", "preview"), ("History…", "history"), ("Settings…", "settings"),
        ("New session", "reset"),
        None, ("Check for updates…", "update"), ("Setup…", "setup"), ("Take the tour", "tour"), ("Quit Flippy", "quit")]


def _pixmaps(style=None):
    """IconPixmap: [(w, h, ARGB32 bytes, network byte order, not premultiplied)]."""
    out = []
    for px in ICON_PX:
        surf = statusicon.light(style, px)
        data, stride = bytes(surf.get_data()), surf.get_stride()
        argb = bytearray()
        for y in range(px):
            row = data[y * stride:y * stride + px * 4]
            for x in range(0, len(row), 4):
                b, g, r, a = row[x:x + 4]  # cairo: native-endian premultiplied (little-endian here)
                if a:
                    r, g, b = (min(255, c * 255 // a) for c in (r, g, b))
                argb += bytes((a, r, g, b))
        out.append(dbus.Struct((dbus.Int32(px), dbus.Int32(px), dbus.ByteArray(bytes(argb))), signature="iiay"))
    return dbus.Array(out, signature="(iiay)")


class Menu(dbus.service.Object):
    def __init__(self, bus, tray):
        super().__init__(bus, MENU_PATH)
        self.tray = tray
        self.revision = 1
        self.items = self._items()

    def _items(self):
        """{id: properties} for the menu as it should look right now; id 0 is the root."""
        mode = settings.get("help", "mode")
        app, name = self.tray.front
        shortcuts = self.tray.shortcuts()
        items = {}
        for i, entry in enumerate(MENU, start=1):
            if entry is None:
                items[i] = {"type": "separator"}
                continue
            title, cmd = entry
            props = {"label": title, "enabled": True}
            if cmd in shortcuts:
                props["label"] = f"{title}    {shortcuts[cmd]}"
            if cmd in ("help-toggle", "tips-toggle"):
                props["toggle-type"] = "checkmark"
                props["toggle-state"] = int(mode != "off" if cmd == "help-toggle" else mode == "tips")
            elif cmd == "watch-front":
                props["toggle-type"] = "checkmark"
                props["toggle-state"] = int(bool(app) and app in settings.get_list("help", "apps"))
                props["label"] = f"Watch {name}" if app else "Watch this app"
                props["enabled"] = bool(app)
            elif cmd == "goal-front":
                props["label"] = f"Set a goal for {name}…" if app else "Set a goal…"
                props["enabled"] = bool(app)
            items[i] = props
        return items

    def refresh(self):
        """Rebuild; tell the panel if anything changed. Returns whether it did."""
        new = self._items()
        if new == self.items:
            return False
        self.items = new
        self.revision += 1
        self.LayoutUpdated(dbus.UInt32(self.revision), dbus.Int32(0))
        return True

    @staticmethod
    def _props(props, names=()):
        return dbus.Dictionary({k: v for k, v in props.items() if not names or k in names}, signature="sv")

    def _node(self, i, names):
        if i == 0:
            kids = [dbus.Struct((dbus.Int32(k), self._props(p, names), dbus.Array([], signature="v")), signature="ia{sv}av")
                    for k, p in self.items.items()]
            return dbus.Struct((dbus.Int32(0), self._props({"children-display": "submenu"}, names),
                                dbus.Array(kids, signature="v")), signature="ia{sv}av")
        return dbus.Struct((dbus.Int32(i), self._props(self.items.get(i, {}), names), dbus.Array([], signature="v")),
                           signature="ia{sv}av")

    @dbus.service.method(MENU_IFACE, in_signature="iias", out_signature="u(ia{sv}av)")
    def GetLayout(self, parent, depth, names):
        if int(parent) == 0:  # (re)reading the whole menu, likely to show it: label it for the app in front
            self.tray.front = sensors.frontmost()
            new = self._items()
            if new != self.items:
                self.items, self.revision = new, self.revision + 1
        return dbus.UInt32(self.revision), self._node(int(parent), list(names))

    @dbus.service.method(MENU_IFACE, in_signature="aias", out_signature="a(ia{sv})")
    def GetGroupProperties(self, ids, names):
        ids = [int(i) for i in ids] or list(self.items)
        return dbus.Array([dbus.Struct((dbus.Int32(i), self._props(self.items.get(i, {}), list(names))),
                                       signature="ia{sv}") for i in ids if i in self.items], signature="(ia{sv})")

    @dbus.service.method(MENU_IFACE, in_signature="is", out_signature="v")
    def GetProperty(self, i, name):
        return self.items.get(int(i), {}).get(str(name), "")

    @dbus.service.method(MENU_IFACE, in_signature="isvu", out_signature="")
    def Event(self, i, event_id, data, timestamp):
        if str(event_id) == "clicked":
            self.tray.clicked(int(i))
        elif str(event_id) == "opened":
            self.tray.opening()

    @dbus.service.method(MENU_IFACE, in_signature="a(isvu)", out_signature="ai")
    def EventGroup(self, events):
        for i, event_id, data, ts in events:
            self.Event(i, event_id, data, ts)
        return dbus.Array([], signature="i")

    @dbus.service.method(MENU_IFACE, in_signature="i", out_signature="b")
    def AboutToShow(self, i):
        return self.tray.opening()

    @dbus.service.method(MENU_IFACE, in_signature="ai", out_signature="aiai")
    def AboutToShowGroup(self, ids):
        changed = self.tray.opening()
        return dbus.Array([0] if changed else [], signature="i"), dbus.Array([], signature="i")

    @dbus.service.signal(MENU_IFACE, signature="ui")
    def LayoutUpdated(self, revision, parent):
        pass

    @dbus.service.signal(MENU_IFACE, signature="a(ia{sv})a(ias)")
    def ItemsPropertiesUpdated(self, updated, removed):
        pass

    @dbus.service.method(PROPS_IFACE, in_signature="ss", out_signature="v")
    def Get(self, iface, name):
        return self.GetAll(iface)[name]

    @dbus.service.method(PROPS_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, iface):
        return dbus.Dictionary({"Version": dbus.UInt32(3), "TextDirection": "ltr", "Status": "normal",
                                "IconThemePath": dbus.Array([], signature="s")}, signature="sv")


class Item(dbus.service.Object):
    def __init__(self, bus, tray):
        super().__init__(bus, ITEM_PATH)
        self.tray = tray
        self.pixmaps = _pixmaps()

    def props(self):
        return {"Category": "ApplicationStatus", "Id": "flippy", "Title": "Flippy", "Status": "Active",
                "WindowId": dbus.Int32(0), "IconName": "", "IconPixmap": self.pixmaps,
                "OverlayIconName": "", "OverlayIconPixmap": dbus.Array([], signature="(iiay)"),
                "AttentionIconName": "", "AttentionIconPixmap": dbus.Array([], signature="(iiay)"),
                "AttentionMovieName": "", "IconThemePath": "",
                "ToolTip": dbus.Struct(("", dbus.Array([], signature="(iiay)"), "Flippy", "Ask about your screen"),
                                       signature="sa(iiay)ss"),
                "ItemIsMenu": True, "Menu": dbus.ObjectPath(MENU_PATH)}

    def set_icon(self):
        self.pixmaps = _pixmaps()
        self.NewIcon()

    @dbus.service.method(PROPS_IFACE, in_signature="ss", out_signature="v")
    def Get(self, iface, name):
        return self.props()[str(name)]

    @dbus.service.method(PROPS_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, iface):
        return dbus.Dictionary(self.props(), signature="sv")

    @dbus.service.method(SNI_IFACE, in_signature="ii", out_signature="")
    def Activate(self, x, y):
        self.tray.command("ask")

    @dbus.service.method(SNI_IFACE, in_signature="ii", out_signature="")
    def SecondaryActivate(self, x, y):
        self.tray.command("draw")

    @dbus.service.method(SNI_IFACE, in_signature="ii", out_signature="")
    def ContextMenu(self, x, y):
        pass  # the panel shows Menu itself

    @dbus.service.method(SNI_IFACE, in_signature="is", out_signature="")
    def Scroll(self, delta, orientation):
        pass

    @dbus.service.signal(SNI_IFACE, signature="")
    def NewIcon(self):
        pass


class Tray:
    """platform: the Linux Platform (command(), open_setup(), ask_goal())."""

    def __init__(self, platform):
        self.platform = platform
        self.front = (None, None)
        self.bus = dbus.SessionBus()
        self.name = dbus.service.BusName(f"org.kde.StatusNotifierItem-{os.getpid()}-1", self.bus)
        self.menu = Menu(self.bus, self)
        self.item = Item(self.bus, self)
        settings.on_change(self._on_setting)
        self._register()
        # the panel restarting (or starting after us) brings a new watcher: register again
        self.bus.add_signal_receiver(lambda name, old, new: name == "org.kde.StatusNotifierWatcher" and new
                                     and self._register(), signal_name="NameOwnerChanged",
                                     dbus_interface="org.freedesktop.DBus", arg0="org.kde.StatusNotifierWatcher")

    def _register(self):
        try:
            watcher = self.bus.get_object("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher")
            watcher.RegisterStatusNotifierItem(self.name.get_name(), dbus_interface="org.kde.StatusNotifierWatcher",
                                               reply_handler=lambda *a: None,
                                               error_handler=lambda e: print(f"flippy: tray: {e}", flush=True))
        except dbus.DBusException as e:
            print(f"flippy: tray: no StatusNotifierWatcher ({e})", flush=True)

    def command(self, cmd):
        return self.platform.command(cmd)

    def shortcuts(self):
        from .setup import flippy_shortcuts, pretty_accel
        out = {}
        for accel, args in flippy_shortcuts():
            out.setdefault({"": "ask"}.get(args, args), pretty_accel(accel))
        # a shortcut COSMIC also uses gets a warning sign next to it
        clashes = self.platform.shortcut_problems()
        for name, label in list(out.items()):
            if clashes.get({"pause-toggle": "pause"}.get(name, name)):
                out[name] = label + "  ⚠"
        return out

    def opening(self):
        """The menu is about to open: the active window is the app they're in."""
        self.front = sensors.frontmost()
        return self.menu.refresh()

    def clicked(self, i):
        entry = MENU[i - 1] if 0 < i <= len(MENU) else None
        if not entry:
            return
        cmd = entry[1]
        if cmd == "help-toggle":
            self.command(f"help-mode {'off' if settings.get('help', 'mode') != 'off' else 'quiet'}")
        elif cmd == "tips-toggle":
            self.command(f"help-mode {'quiet' if settings.get('help', 'mode') == 'tips' else 'tips'}")
        elif cmd == "watch-front":
            if self.front[0]:
                self.command(f"watch-app {self.front[0]}")
        elif cmd == "goal-front":
            if self.front[0]:
                self.platform.ask_goal(*self.front)
        else:
            self.command(cmd)
        self.menu.refresh()

    def _on_setting(self, section, key, value):
        if section == "look" and key in ("pointer", "theme"):
            try:
                self.item.set_icon()
            except Exception as e:  # never lose the tray over a drawing problem
                print(f"flippy: tray icon: {e}", flush=True)
        elif section == "help":
            self.menu.refresh()
