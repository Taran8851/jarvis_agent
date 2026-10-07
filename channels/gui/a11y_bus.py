#!/usr/bin/python3
"""Accessibility bus for the background session, in place of at-spi-bus-launcher.

The stock launcher puts its socket at $XDG_RUNTIME_DIR/at-spi/bus (or bus_<n> when DISPLAY is
:<n>) and offers no way to choose the name. In the shared runtime dir that name is the real
session's, and the Firefox snap can only open <runtime>/at-spi/bus*, so neither a separate
directory nor the default name works. This starts the same dbus-daemon the launcher would, on a
socket name of our choosing, and answers org.a11y.Bus on the hidden session bus so apps find it.

Run inside the background session by bg.py: python3 a11y_bus.py SOCKET_PATH
"""
import subprocess
import sys

from gi.repository import Gio, GLib

XML = """<node>
  <interface name="org.a11y.Bus">
    <method name="GetAddress"><arg type="s" name="address" direction="out"/></method>
  </interface>
  <interface name="org.a11y.Status">
    <property name="IsEnabled" type="b" access="readwrite"/>
    <property name="ScreenReaderEnabled" type="b" access="readwrite"/>
  </interface>
</node>"""


def main():
    address = f"unix:path={sys.argv[1]}"
    daemon = subprocess.Popen(["dbus-daemon", "--config-file=/usr/share/defaults/at-spi2/accessibility.conf",
                               "--nofork", "--nopidfile", f"--address={address}"])
    state = {"IsEnabled": True, "ScreenReaderEnabled": False}
    loop = GLib.MainLoop()

    def on_call(conn, sender, path, iface, method, params, invocation):
        invocation.return_value(GLib.Variant("(s)", (address,)))

    def on_get(conn, sender, path, iface, prop):
        return GLib.Variant("b", state[prop])

    def on_set(conn, sender, path, iface, prop, value):
        state[prop] = value.unpack()
        return True

    def on_bus(conn, name):
        node = Gio.DBusNodeInfo.new_for_xml(XML)
        for iface in node.interfaces:
            conn.register_object("/org/a11y/bus", iface, on_call, on_get, on_set)

    Gio.bus_own_name(Gio.BusType.SESSION, "org.a11y.Bus", Gio.BusNameOwnerFlags.NONE, on_bus, None, lambda *a: loop.quit())
    GLib.child_watch_add(GLib.PRIORITY_DEFAULT, daemon.pid, lambda *a: loop.quit())
    try:
        loop.run()
    finally:
        daemon.terminate()


if __name__ == "__main__":
    main()
