"""Tray icon with the headset's battery level; click it to open blackctl.

Uses StatusNotifierItem over D-Bus (waybar, Quickshell, KDE, ...) when a tray host is running,
otherwise a classic X11 tray icon (polybar, trayer, ...). The icon is only there while a
headset is connected.
"""

import os
import subprocess
import sys

import gi
from gi.repository import Gio, GLib

from .device import Device

POLL_SECONDS = 10
WATCHER = 'org.kde.StatusNotifierWatcher'
ITEM_PATH = '/StatusNotifierItem'
ITEM_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <signal name="NewIcon"/>
    <signal name="NewToolTip"/>
  </interface>
</node>
"""


def headset_state():
    """(icon name, tooltip) for the connected headset, or None when there is none."""
    dev = Device.detect()
    if not dev:
        return None
    level = dev.read_int('battery')
    if level is None:
        return 'audio-headphones-symbolic', f'{dev.name}: battery not reported yet'
    charging = dev.read_int('charging') == 1
    step = min(100, round(level / 10) * 10)
    icon = f'battery-level-{step}-charging-symbolic' if charging else f'battery-level-{step}-symbolic'
    return icon, f'{dev.name}: {level}%' + (' (charging)' if charging else '')


def open_app():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    subprocess.Popen([sys.executable, '-m', 'blackctl'], cwd=root, start_new_session=True)


class StatusNotifierItem:
    """Registers with the tray host while a headset is connected, drops out when it isn't."""

    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.name = f'org.kde.StatusNotifierItem-{os.getpid()}-1'
        self.owner_id = 0
        self.state = None
        info = Gio.DBusNodeInfo.new_for_xml(ITEM_XML).interfaces[0]
        self.bus.register_object(ITEM_PATH, info, self._call, self._property, None)
        # Register again whenever the tray host (re)starts.
        Gio.bus_watch_name_on_connection(self.bus, WATCHER, Gio.BusNameWatcherFlags.NONE,
                                         lambda *_: self._register(), None)

    def _call(self, _bus, _sender, _path, _iface, method, _params, invocation):
        if method in ('Activate', 'SecondaryActivate', 'ContextMenu'):
            open_app()
        invocation.return_value(None)

    def _property(self, _bus, _sender, _path, _iface, prop):
        icon, tooltip = self.state or ('audio-headphones-symbolic', '')
        values = {
            'Category': GLib.Variant('s', 'Hardware'),
            'Id': GLib.Variant('s', 'blackctl'),
            'Title': GLib.Variant('s', tooltip),
            'Status': GLib.Variant('s', 'Active'),
            'IconName': GLib.Variant('s', icon),
            'ToolTip': GLib.Variant('(sa(iiay)ss)', (icon, [], 'blackctl', tooltip)),
            'ItemIsMenu': GLib.Variant('b', False),
            'Menu': GLib.Variant('o', '/NO_DBUSMENU'),
        }
        return values.get(prop)

    def _register(self):
        if self.owner_id:
            self.bus.call(WATCHER, '/StatusNotifierWatcher', WATCHER, 'RegisterStatusNotifierItem',
                          GLib.Variant('(s)', (self.name,)), None, Gio.DBusCallFlags.NONE, -1, None)

    def update(self, state):
        if state and not self.owner_id:
            self.state = state
            self.owner_id = Gio.bus_own_name_on_connection(
                self.bus, self.name, Gio.BusNameOwnerFlags.NONE, lambda *_: self._register(), None)
        elif not state and self.owner_id:
            Gio.bus_unown_name(self.owner_id)
            self.owner_id = 0
        elif state != self.state:
            self.state = state
            for signal in ('NewIcon', 'NewToolTip'):
                self.bus.emit_signal(None, ITEM_PATH, 'org.kde.StatusNotifierItem', signal, None)
        self.state = state


class XembedIcon:
    """X11 system tray icon for bars without a StatusNotifierItem host."""

    def __init__(self):
        gi.require_version('Gtk', '3.0')
        from gi.repository import Gtk
        self.icon = Gtk.StatusIcon()
        self.icon.set_title('blackctl')
        self.icon.connect('activate', lambda _i: open_app())
        self.icon.connect('popup-menu', lambda *_: open_app())

    def update(self, state):
        self.icon.set_visible(state is not None)
        if state:
            self.icon.set_from_icon_name(state[0])
            self.icon.set_tooltip_text(state[1])


def main():
    bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    has_host = bus.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
                             'NameHasOwner', GLib.Variant('(s)', (WATCHER,)), None,
                             Gio.DBusCallFlags.NONE, -1, None).unpack()[0]
    x11_only = os.environ.get('DISPLAY') and not os.environ.get('WAYLAND_DISPLAY')
    tray = XembedIcon() if x11_only and not has_host else StatusNotifierItem()

    def tick():
        tray.update(headset_state())
        return True
    tick()
    GLib.timeout_add_seconds(POLL_SECONDS, tick)
    GLib.MainLoop().run()
