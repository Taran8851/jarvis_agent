#!/usr/bin/python3
"""Screenshot-free GUI control for GNOME/Wayland: read and act through the accessibility tree.

Elements are found by app + role + name and driven by their own AT-SPI action ("press",
"switch", "click"), so no pointer position, coordinate scaling or screenshot is involved.
Keys go through ydotool (uinput) and land on whichever window has focus.

Baseline for the eval in evals/gui/ -- stdlib + the system Atspi bindings (python3-gi,
gir1.2-atspi-2.0), nothing installed. CLI: `python3 gui.py --help`.
"""
import argparse
import fcntl
import json
import os
import struct
import subprocess
import sys
import time
import warnings

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402

# get_action_name is the only binding that returns an action's name here; its replacement
# (get_name) is not exposed by this Atspi version.
warnings.filterwarnings("ignore", category=DeprecationWarning, message=".*get_action_name.*")

# "bg" when this process was started inside the background session (bg.py sets it together with
# that session's bus and display). The accessibility tree follows the process's session bus, so
# a session is a property of the process, not of a call.
BG = os.environ.get("JARVIS_GUI_SESSION") == "bg"
MAX_NODES = 20000  # a runaway tree (Firefox with many tabs is ~4k) stops here instead of hanging
MAX_KIDS = 500   # children read per element
YDOTOOL_SOCKET = os.environ.get("YDOTOOL_SOCKET", f"/run/user/{os.getuid()}/.ydotool_socket")

# evdev key codes (linux/input-event-codes.h) for the names `key()` accepts.
KEYS = {
    "esc": 1, "backspace": 14, "tab": 15, "enter": 28, "ctrl": 29, "shift": 42, "alt": 56, "space": 57,
    "super": 125, "home": 102, "up": 103, "pageup": 104, "left": 105, "right": 106, "end": 107,
    "down": 108, "pagedown": 109, "delete": 111,
    **{c: k for c, k in zip("1234567890", range(2, 12))},
    **{c: k for c, k in zip("qwertyuiop", range(16, 26))},
    **{c: k for c, k in zip("asdfghjkl", range(30, 39))},
    **{c: k for c, k in zip("zxcvbnm", range(44, 51))},
    **{f"f{i}": 58 + i for i in range(1, 11)},
    "f11": 87, "f12": 88, "insert": 110, "menu": 127,   # menu: the context-menu key
    "escape": 1, "return": 28, "del": 111, "pgup": 104, "pgdn": 109,   # names models reach for
}


def apps():
    d = Atspi.get_desktop(0)
    return [a for a in (d.get_child_at_index(i) for i in range(d.get_child_count())) if a]


def app_instances(name):
    """Every running application with this name (case-insensitive). There can be several: each
    zenity dialog is its own process, and GTK apps often register a second, empty entry."""
    hits = [a for a in apps() if (a.get_name() or "").lower() == name.lower()]
    if not hits:
        raise LookupError(f"no app named {name!r}; running: {sorted({a.get_name() for a in apps()})}")
    return hits


def walk(node):
    """Depth-first over the subtree, skipping nodes that error (apps drop objects mid-walk)."""
    stack, n = [node], 0
    while stack and n < MAX_NODES:
        cur = stack.pop()
        n += 1
        yield cur
        try:
            # capped: a spreadsheet's table reports every cell of the sheet as a child (10^10)
            kids = [cur.get_child_at_index(i) for i in range(min(cur.get_child_count(), MAX_KIDS))]
        except Exception:
            continue
        stack.extend(k for k in reversed(kids) if k)


def has(el, state):
    return el.get_state_set().contains(getattr(Atspi.StateType, state.upper()))


def actions(el):
    try:
        return [Atspi.Action.get_action_name(el, i) for i in range(el.get_n_actions())]
    except Exception:
        return []


def windows(app_name):
    out = []
    for root in app_instances(app_name):
        root.clear_cache()  # this process has no event loop, so cached names/states go stale
        out += [w for w in (root.get_child_at_index(i) for i in range(root.get_child_count())) if w]
    return out


def window(app_name, title):
    """The one window whose title contains `title`. Refuses to guess between several: a loose
    match ("Mozilla Firefox") would otherwise act on whichever window came first."""
    hits = [w for w in windows(app_name) if title.lower() in (w.get_name() or "").lower()]
    if len(hits) != 1:
        raise LookupError(f"{len(hits)} {app_name} windows match {title!r}: {[w.get_name() for w in hits]}")
    return hits[0]


def labels(el):
    """The strings an element can be found by: its name, its description (an icon-only button
    is named by its glyph and carries its tooltip there) and, when it has neither, the first
    label a few levels inside it (GTK4 menu items and dropdown rows are unnamed wrappers)."""
    out = [el.get_name() or "", el.get_description() or ""]
    if all(out):
        out.append(f"{out[0]} ({out[1]})")  # the form tree() prints; a model copies it whole
    if not any(out):
        level = [el]
        for _ in range(3):
            level = [k for p in level for k in (p.get_child_at_index(i) for i in range(min(p.get_child_count(), 4))) if k]
            found = [k.get_name() for k in level if k.get_role_name() == "label" and k.get_name()]
            if found:
                out.append(found[0])
                break
    return [x for x in out if x]


def _norm(s):
    """Case and whitespace folded: real pages put no-break and thin spaces inside names."""
    return " ".join(s.split()).casefold()


def find(app_name, role=None, name=None, window=None, exact=False, showing=True):
    """Elements under an app, filtered by role and name (substring unless exact; matched against
    everything in labels()). `window` is a title substring or an element (a window from
    windows(), or any container to search inside); a held element keeps pointing at the same
    window when its title changes. Only what is on screen, unless showing=False. Best name
    match first, then document order."""
    if window is None:
        tops = windows(app_name)
    elif isinstance(window, str):
        tops = [globals()["window"](app_name, window)]
    else:
        window.clear_cache()
        tops = [window]
    out, want = [], _norm(name) if name is not None else None
    for top in tops:
        for el in walk(top):
            try:
                if role and el.get_role_name() != role:
                    continue
                rank = 0
                if name is not None:
                    names = [_norm(n) for n in labels(el)]
                    # exact beats "starts with" beats "contains": on a real page "Issues" must
                    # find the "Issues 2k" tab before the "All issues" link that precedes it
                    rank = 0 if want in names else 1 if any(n.startswith(want) for n in names) else 2
                    if (exact and rank) or not any(want in n for n in names):
                        continue
                if showing and not has(el, "showing"):
                    continue
                out.append((rank, el))
            except Exception:
                continue
    return [el for _, el in sorted(out, key=lambda x: x[0])]


def one(app_name, **kw):
    """The best match on screen; failing that, one that is scrolled out of view, brought into
    view first (a long page's "More" link is in the tree but not showing)."""
    hits = find(app_name, **kw)
    if not hits and kw.get("showing", True):
        hits = find(app_name, **{**kw, "showing": False})
        if hits:
            try:
                scroll_to(hits[0])
                time.sleep(0.3)
            except Exception:
                pass  # GTK4 refuses scroll_to; its actions work off screen anyway
    if not hits:
        raise LookupError(f"nothing in {app_name} matches {kw}")
    return hits[0]


def act(el, action=None):
    """Run the element's own action: the named one, else its first. A disabled element is
    refused: toolkits accept the call and silently do nothing."""
    if not (has(el, "enabled") or has(el, "sensitive")):  # Gecko sets both, GTK4 only "sensitive"
        raise RuntimeError(f"{describe(el)} is disabled")
    names = actions(el)
    if not names:
        raise LookupError(f"{describe(el)} has no actions")
    i = names.index(action) if action else 0
    return Atspi.Action.do_action(el, i)


def select(el):
    """Select a row/item through its container's Selection interface. GTK list rows have no
    action of their own (only "scroll-to"), so this is how a row gets picked."""
    parent = el.get_parent()
    if "Selection" not in parent.get_interfaces():
        raise LookupError(f"{describe(el)} is not in a selectable container")
    return Atspi.Selection.select_child(parent, el.get_index_in_parent())


def text(el):
    try:
        return Atspi.Text.get_text(el, 0, Atspi.Text.get_character_count(el))
    except Exception:
        return ""


def set_text(el, value):
    return Atspi.EditableText.set_text_contents(el, value)


def focus(el):
    return Atspi.Component.grab_focus(el)


def scroll_to(el):
    """Bring an off-screen element into view (it then passes the showing filter)."""
    return Atspi.Component.scroll_to(el, Atspi.ScrollType.ANYWHERE)


def set_value(el, value):
    """Sliders, spinners, progress-like controls."""
    return Atspi.Value.set_current_value(el, float(value))


def window_of(el):
    """The top-level window an element sits in: its last ancestor before the application."""
    while (parent := el.get_parent()) and parent.get_role_name() != "application":
        el = parent
    return el


def describe(el):
    names = labels(el)
    d = {"role": el.get_role_name(), "name": names[0] if names else ""}
    if len(names) > 1:
        d["description"] = names[1]
    if a := actions(el):
        d["actions"] = a
    if t := text(el):
        d["text"] = t[:200]
    st = [s for s in ("focused", "selected", "checked", "expanded", "active") if has(el, s)]
    if st:
        d["states"] = st
    return d


def tree(app_name, window=None, showing=True):
    """What a model needs to pick a target: one line per element that has a name and either an
    action or text. On-screen only by default; showing=False gives the whole page."""
    lines = []
    for el in find(app_name, window=window, showing=showing):
        d = describe(el)
        if d["name"] and ("actions" in d or "text" in d):
            label = d["name"][:80] + (f' ({d["description"][:60]})' if "description" in d else "")
            lines.append(f'{d["role"]} "{label}"' + (f' [{",".join(d["states"])}]' if "states" in d else ""))
    return "\n".join(lines)


def locked():
    if BG:
        return False  # the background session has locking disabled and no physical seat
    out = subprocess.run(["gdbus", "call", "--session", "--dest", "org.gnome.ScreenSaver", "--object-path",
                          "/org/gnome/ScreenSaver", "--method", "org.gnome.ScreenSaver.GetActive"],
                         capture_output=True, text=True, timeout=5).stdout
    return "true" in out


SHELL = "dev.avifenesh.ComputerUseLinux.WindowControl"  # GNOME Shell extension (computer-use-linux)
SHELL_PATH = "/dev/avifenesh/ComputerUseLinux/WindowControl"


def _shell(method, *args):
    out = subprocess.run(["busctl", "--user", "--json=short", "call", SHELL, SHELL_PATH, SHELL, method, *args],
                         capture_output=True, text=True, timeout=10)
    if out.returncode:
        raise RuntimeError(f"window control unavailable ({out.stderr.strip()}); the extension unloads on a locked screen")
    return json.loads(out.stdout)["data"]


def shell_windows():
    """Mutter's own window list: ids, titles, pids and which one really has focus (AT-SPI's
    "active" state is not reliable on Wayland)."""
    return json.loads(_shell("ListWindows")[0])


def shell_window(win):
    """The compositor's record for an AT-SPI window, matched by pid + title."""
    win.clear_cache()
    title, pid = win.get_name() or "", win.get_process_id()
    hits = [w for w in shell_windows() if w["title"] == title and w["pid"] == pid]
    if len(hits) != 1:
        raise LookupError(f"{len(hits)} compositor windows match {title!r} (pid {pid})")
    return hits[0]


def activate(win):
    """Raise and focus the window behind an AT-SPI frame. AT-SPI cannot do this on Wayland; the
    compositor has to."""
    if BG:
        # The hidden seat has no keyboard until the RemoteDesktop session creates one, and a
        # window activated before that never receives keyboard focus.
        _RemoteDesktop.get()
    wid = shell_window(win)["window_id"]
    _shell("ActivateWindow", "t", str(wid))
    for _ in range(20):
        if any(w["window_id"] == wid and w["focused"] for w in shell_windows()):
            return True
        time.sleep(0.05)
    raise RuntimeError(f"{win.get_name()!r} did not take focus")


class _RemoteDesktop:
    """Input for the background session: Mutter's RemoteDesktop interface on that session's
    bus. One session per process, on one connection: Mutter ends it when the caller leaves the
    bus, so it cannot be driven by one-shot busctl calls."""
    NAME, PATH = "org.gnome.Mutter.RemoteDesktop", "/org/gnome/Mutter/RemoteDesktop"
    _one = None

    @classmethod
    def get(cls):
        if cls._one is None:
            cls._one = cls()
        return cls._one

    def __init__(self):
        from gi.repository import Gio, GLib
        self.Gio, self.GLib = Gio, GLib
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.session = self._call(self.PATH, self.NAME, "CreateSession", None, "(o)").unpack()[0]
        self._call(self.session, self.NAME + ".Session", "Start")
        # Mutter creates each virtual device on its first event, and that event is dropped; a
        # window also gets no keyboard focus until the keyboard exists. A Shift tap brings it up.
        for down in (True, False):
            self.keycode(KEYS["shift"], down)
        self.motion(1, 0)   # the same for the pointer: its first event only creates the device
        self.motion(-1, 0)
        time.sleep(0.6)

    def _call(self, path, iface, method, args=None, ret=None):
        return self.bus.call_sync(self.NAME, path, iface, method, args, self.GLib.VariantType(ret) if ret else None,
                                  self.Gio.DBusCallFlags.NONE, 5000, None)

    def _notify(self, method, sig, *values):
        self._call(self.session, self.NAME + ".Session", method, self.GLib.Variant(sig, values))

    def keycode(self, code, down):
        self._notify("NotifyKeyboardKeycode", "(ub)", code, down)

    def keysym(self, sym, down):
        self._notify("NotifyKeyboardKeysym", "(ub)", sym, down)

    def motion(self, dx, dy):
        self._notify("NotifyPointerMotionRelative", "(dd)", float(dx), float(dy))

    def button(self, code, down):
        self._notify("NotifyPointerButton", "(ib)", code, down)

    def paste(self, text, send_paste, wait=1.5):
        """Put `text` on this session's clipboard and paste it: the route for characters the
        keyboard layout does not have (a keysym outside the layout is dropped by Mutter).
        `send_paste` presses the paste shortcut; the text is handed over when the app asks."""
        iface, served = self.NAME + ".Session", []
        if not getattr(self, "_clipboard", False):
            self._notify("EnableClipboard", "(a{sv})", {})
            self._clipboard = True

        def on_transfer(conn, sender, path, iface_, signal, params):
            mime, serial = params.unpack()
            reply, fds = self.bus.call_with_unix_fd_list_sync(
                self.NAME, self.session, iface, "SelectionWrite", self.GLib.Variant("(u)", (serial,)),
                self.GLib.VariantType("(h)"), self.Gio.DBusCallFlags.NONE, 5000, None, None)
            fd = fds.get(reply.unpack()[0])
            try:
                os.write(fd, text.encode())
            finally:
                os.close(fd)
            self._notify("SelectionWriteDone", "(ub)", serial, True)
            served.append(mime)

        sub = self.bus.signal_subscribe(self.NAME, iface, "SelectionTransfer", self.session, None,
                                        self.Gio.DBusSignalFlags.NONE, on_transfer)
        try:
            mimes = self.GLib.Variant("as", ["text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "STRING"])
            self._notify("SetSelection", "(a{sv})", {"mime-types": mimes})
            send_paste()
            ctx, end = self.GLib.MainContext.default(), time.monotonic() + wait
            while time.monotonic() < end:      # an app may ask more than once, for several types
                ctx.iteration(False)
                time.sleep(0.01)
        finally:
            self.bus.signal_unsubscribe(sub)
        if not served:
            raise RuntimeError("text not pasted: the focused app never asked for the clipboard")


_KEYSYMS = {"\n": 0xFF0D, "\t": 0xFF09}  # everything else printable in ASCII is its own keysym


class UserPresent(RuntimeError):
    """The owner is at the keyboard: foreground input would land in whatever the owner is doing."""


_OWN_INPUT = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "jarvis-gui-last-input")


def idle_seconds():
    """Seconds since the real seat last saw input (ours included)."""
    out = subprocess.run(["gdbus", "call", "--session", "--dest", "org.gnome.Mutter.IdleMonitor", "--object-path",
                          "/org/gnome/Mutter/IdleMonitor/Core", "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime"],
                         capture_output=True, text=True, timeout=5).stdout
    return int(out.split()[1].rstrip(",)")) / 1000


def _guard():
    """Before every foreground key or pointer action: has someone else touched the machine?
    Our own input resets the idle clock too, so the time of our last action is kept (in a file:
    each CLI call is a new process) and only input newer than that counts as them."""
    if BG:
        return
    idle, now = idle_seconds(), time.clock_gettime(time.CLOCK_MONOTONIC)
    try:
        since_ours = now - float(open(_OWN_INPUT).read())
    except (OSError, ValueError):
        since_ours = None
    recent = idle < 2.0 if since_ours is None or since_ours > 600 else idle < since_ours - 0.5
    if recent:
        raise UserPresent(f"paused: input {idle:.1f}s ago that was not ours; you are at the keyboard")


def _sent():
    with open(_OWN_INPUT, "w") as f:
        f.write(str(time.clock_gettime(time.CLOCK_MONOTONIC)))


def _ydotool(*args):
    # uinput keys go to whatever holds keyboard focus; on a locked screen that is the password box.
    if locked():
        raise RuntimeError("screen is locked: keys would go to the lock screen")
    _guard()
    subprocess.run(["ydotool", *args], check=True, capture_output=True, timeout=20,
                   env=dict(os.environ, YDOTOOL_SOCKET=YDOTOOL_SOCKET))
    _sent()


def key(combo, window=None):
    """'ctrl+l', 'enter', 'ctrl+shift+t': press in order, release in reverse. Keys land on the
    focused window, so pass the target `window` to have it activated first."""
    if window is not None:
        activate(window)
    unknown = [k for k in combo.lower().split("+") if k not in KEYS]
    if unknown:
        raise LookupError(f"unknown key {unknown}; names: {' '.join(sorted(k for k in KEYS if len(k) > 1))}, letters, digits")
    codes = [KEYS[k] for k in combo.lower().split("+")]
    if BG:
        rd = _RemoteDesktop.get()
        for c in codes:
            rd.keycode(c, True)
        for c in reversed(codes):
            rd.keycode(c, False)
        time.sleep(0.05)
        return
    _ydotool("key", *[f"{c}:1" for c in codes], *[f"{c}:0" for c in reversed(codes)])


def type_into(el, s):
    """Type into one field: activate its window, give the field focus, check it took, then send
    keys. The reliable way to enter text in Firefox, where EditableText is a silent no-op."""
    activate(window_of(el))
    try:
        focus(el)
    except Exception:
        pass  # GTK4 text fields reject grab_focus over AT-SPI; the check below decides
    for _ in range(20):
        el.clear_cache()
        if has(el, "focused"):
            return type_text(s)
        time.sleep(0.05)
    raise RuntimeError(f"{describe(el)} did not take focus; no keys sent")


def type_text(s, window=None):
    if window is not None:
        activate(window)
    if BG:
        # keysyms, so the character does not depend on which key carries it; Mutter adds Shift
        # itself. It drops a keysym that is not in the session's layout, so ASCII only for now.
        rd = _RemoteDesktop.get()
        # Pasted, not typed: characters the layout lacks, and anything with line breaks (a code
        # editor auto-indents typed lines, so typed indentation doubles up or gets dropped).
        if not s.isascii() or "\n" in s:
            return rd.paste(s, lambda: key("ctrl+v"))
        for ch in s:
            sym = _KEYSYMS.get(ch, ord(ch))
            rd.keysym(sym, True)
            rd.keysym(sym, False)
            time.sleep(0.004)
        return
    if not s.isascii() or "\n" in s:
        # ydotool types by key position and has no key for these, and typed line breaks fight
        # an editor's auto-indent; same clipboard route as the background, on the real session
        # (this replaces the user's clipboard contents)
        if locked():
            raise RuntimeError("screen is locked: keys would go to the lock screen")
        _guard()
        return _RemoteDesktop.get().paste(s, lambda: _ydotool("key", "29:1", "47:1", "47:0", "29:0"))
    _ydotool("type", "--key-delay", "4", "--", s)


# --- pointer: the last resort, for what has no action and no keyboard route (canvas, drag) -----
#
# ydotool moves the pointer with relative events, which pointer acceleration distorts, so its
# "absolute" moves miss. This creates a uinput device with absolute axes sized to the desktop
# (the way a VM's tablet device works): a position sent is the position reached.

_EV_SYN, _EV_KEY, _EV_ABS = 0, 1, 3
_ABS_X, _ABS_Y = 0, 1
_BUTTONS = {"left": 0x110, "right": 0x111, "middle": 0x112}
_UI_SET_EVBIT, _UI_SET_KEYBIT, _UI_SET_ABSBIT = 0x40045564, 0x40045565, 0x40045567
_UI_DEV_CREATE, _UI_DEV_DESTROY = 0x5501, 0x5502


def desktop_size():
    mons = json.loads(_shell("GetMonitorLayout")[0])
    return max(m["x"] + m["width"] for m in mons), max(m["y"] + m["height"] for m in mons)


class Pointer:
    """`with Pointer() as p: p.move(x, y); p.click()` in logical desktop pixels."""

    def __new__(cls):
        return super().__new__(_BgPointer if BG and cls is Pointer else cls)

    def __init__(self):
        if locked():
            raise RuntimeError("screen is locked: pointer events would go to the lock screen")
        self.w, self.h = desktop_size()

    def __enter__(self):
        _guard()
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        for ev in (_EV_KEY, _EV_ABS):
            fcntl.ioctl(self.fd, _UI_SET_EVBIT, ev)
        for b in _BUTTONS.values():
            fcntl.ioctl(self.fd, _UI_SET_KEYBIT, b)
        for a in (_ABS_X, _ABS_Y):
            fcntl.ioctl(self.fd, _UI_SET_ABSBIT, a)
        absmax, absmin = [0] * 64, [0] * 64
        absmax[_ABS_X], absmax[_ABS_Y] = self.w - 1, self.h - 1
        # struct uinput_user_dev: name[80], input_id (bus, vendor, product, version), ff_effects_max,
        # then absmax, absmin, absfuzz, absflat as 64 ints each
        os.write(self.fd, struct.pack("80sHHHHi", b"jarvis-gui absolute pointer", 0x06, 0x1, 0x1, 1, 0)
                 + struct.pack("64i", *absmax) + struct.pack("64i", *absmin) + bytes(4 * 64 * 2))
        fcntl.ioctl(self.fd, _UI_DEV_CREATE)
        time.sleep(0.7)  # the compositor needs a moment to pick the new device up
        return self

    def __exit__(self, *exc):
        time.sleep(0.1)
        fcntl.ioctl(self.fd, _UI_DEV_DESTROY)
        os.close(self.fd)
        _sent()

    def _emit(self, *events):
        for etype, code, value in (*events, (_EV_SYN, 0, 0)):
            os.write(self.fd, struct.pack("llHHi", 0, 0, etype, code, value))

    def move(self, x, y):
        x, y = min(max(int(x), 0), self.w - 1), min(max(int(y), 0), self.h - 1)
        # two reports: a device's first position, or an unchanged one, can be dropped
        self._emit((_EV_ABS, _ABS_X, max(x - 1, 0)), (_EV_ABS, _ABS_Y, max(y - 1, 0)))
        self._emit((_EV_ABS, _ABS_X, x), (_EV_ABS, _ABS_Y, y))
        time.sleep(0.05)

    def press(self, button="left"):
        self._emit((_EV_KEY, _BUTTONS[button], 1))
        time.sleep(0.03)

    def release(self, button="left"):
        self._emit((_EV_KEY, _BUTTONS[button], 0))
        time.sleep(0.03)

    def click(self, button="left", count=1):
        for _ in range(count):
            self.press(button)
            self.release(button)
            time.sleep(0.05)


class _BgPointer(Pointer):
    """The background session's pointer, over RemoteDesktop. Absolute positions there need a
    screen-cast stream; relative motion needs nothing, and the virtual device applies no
    acceleration, so "into a corner, then by a known offset" lands where intended."""

    def __enter__(self):
        self.rd = _RemoteDesktop.get()
        # home into the bottom-right corner: the top-left one is GNOME's hot corner and would
        # open the overview, which then swallows the first click
        self.rd.motion(10 * self.w, 10 * self.h)
        self.x, self.y = self.w - 1, self.h - 1
        time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        pass

    def move(self, x, y):
        x, y = min(max(int(x), 0), self.w - 1), min(max(int(y), 0), self.h - 1)
        self.rd.motion(x - self.x, y - self.y)
        self.x, self.y = x, y
        time.sleep(0.05)

    def press(self, button="left"):
        self.rd.button(_BUTTONS[button], True)
        time.sleep(0.03)

    def release(self, button="left"):
        self.rd.button(_BUTTONS[button], False)
        time.sleep(0.03)


def point(el, fx=0.5, fy=0.5):
    """Where an element is on the desktop, in logical pixels: (fx, fy) is the spot inside it,
    (0.5, 0.5) its centre. AT-SPI extents on Wayland are relative to the window, so the
    window's origin comes from the compositor; if the toolkit reports device pixels on a
    scaled display, the ratio of the two window widths converts them."""
    win = window_of(el)
    for _ in range(10):  # Firefox answers (-1, -1, -1, -1) until it has laid the element out
        ext = el.get_extents(Atspi.CoordType.WINDOW)
        if ext.width > 0 and ext.height > 0:
            break
        time.sleep(0.1)
        el.clear_cache()
    else:
        raise LookupError(f"{describe(el)} has no position on screen")
    frame = win.get_extents(Atspi.CoordType.WINDOW)
    bounds = shell_window(win)["bounds"]
    scale = frame.width / bounds["width"] if frame.width and bounds["width"] else 1.0
    # An unmaximized window's surface has an invisible shadow margin: the frame then sits at
    # (26, 23) or so inside it, element extents include that inset, and the compositor's
    # bounds do not. Measure elements from the frame, not from the surface.
    return (bounds["x"] + (ext.x - frame.x + ext.width * fx) / scale,
            bounds["y"] + (ext.y - frame.y + ext.height * fy) / scale)


def click(el, button="left", count=1, fx=0.5, fy=0.5):
    """A real pointer click on an element. Use act() when the element has an action; this is
    for a canvas, a spot inside an element, or anything that only reacts to the mouse."""
    activate(window_of(el))
    x, y = point(el, fx, fy)
    with Pointer() as p:
        p.move(x, y)
        p.click(button, count)
    return x, y


def click_at(x, y, button="left", count=1):
    """A pointer click at desktop pixel (x, y): the pixel in a screenshot() picture. Only for
    what the tree does not show."""
    with Pointer() as p:
        p.move(x, y)
        p.click(button, count)
    return x, y


def screenshot(path=None):
    """One picture of the whole desktop -> its path. The fallback when an app shows no elements
    (VLC, a canvas, a slide's text boxes); everything else is cheaper and surer through the tree.

    Background session only. The Shell answers Screenshot only to a few callers it knows by bus
    name; on the hidden session's own bus this process takes one of those names. That is not
    done on the owner's real bus."""
    if not BG:
        raise NotImplementedError("screenshot is only available in the background session")
    from gi.repository import Gio, GLib
    path = os.path.abspath(path or os.path.expanduser(f"~/Jarvis/shots/shot-{time.strftime('%m%d-%H%M%S')}.png"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
                  GLib.Variant("(su)", ("org.gnome.SettingsDaemon.MediaKeys", 4)), None, 0, -1, None)
    time.sleep(0.3)   # the Shell learns the name's owner from a signal
    ok, _ = bus.call_sync("org.gnome.Shell.Screenshot", "/org/gnome/Shell/Screenshot", "org.gnome.Shell.Screenshot",
                          "Screenshot", GLib.Variant("(bbs)", (False, False, path)), None, 0, 15000, None).unpack()
    if not ok:
        raise RuntimeError("the Shell did not write the screenshot")
    return path


def drag(src, dst, steps=12):
    """Press on src, move to dst in small steps (drop targets need to see the motion), release."""
    activate(window_of(src))
    (x0, y0), (x1, y1) = point(src), point(dst)
    with Pointer() as p:
        p.move(x0, y0)
        p.press()
        for i in range(1, steps + 1):
            p.move(x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps)
            time.sleep(0.02)
        time.sleep(0.1)
        p.release()
    return (x0, y0), (x1, y1)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("apps")
    for c in ("tree", "find", "act", "text", "set-text"):
        s = sub.add_parser(c)
        s.add_argument("app")
        s.add_argument("--window")
        if c in ("tree", "find"):
            s.add_argument("--all", action="store_true", help="include what is scrolled out of view")
        if c != "tree":
            s.add_argument("--role")
            s.add_argument("--name")
            s.add_argument("--exact", action="store_true")
        if c == "act":
            s.add_argument("--action")
        if c == "set-text":
            s.add_argument("value")
    for c in ("type-into", "click"):
        s = sub.add_parser(c)
        s.add_argument("app")
        s.add_argument("--window")
        s.add_argument("--role")
        s.add_argument("--name")
        s.add_argument("--exact", action="store_true")
        if c == "type-into":
            s.add_argument("string", nargs="?")
            s.add_argument("--from-file", help="take the text from this file (for code and other multi-line text)")
            s.add_argument("--enter", action="store_true")
        else:
            s.add_argument("--button", default="left")
            s.add_argument("--count", type=int, default=1)
    sub.add_parser("windows").add_argument("app")
    s = sub.add_parser("close")
    s.add_argument("app")
    s.add_argument("--window", required=True)
    sub.add_parser("key").add_argument("combo")
    sub.add_parser("screenshot").add_argument("path", nargs="?")
    s = sub.add_parser("click-at")
    s.add_argument("x", type=float)
    s.add_argument("y", type=float)
    s.add_argument("--button", default="left")
    s.add_argument("--count", type=int, default=1)
    sub.add_parser("type").add_argument("string")
    a = p.parse_args()

    if a.cmd == "apps":
        print("\n".join(sorted({x.get_name() for x in apps() if x.get_child_count()})))
    elif a.cmd == "tree":
        print(tree(a.app, a.window, showing=not a.all))
    elif a.cmd == "close":
        win = window(a.app, a.window)
        title = win.get_name()
        # the window's own close action where it has one (GTK), else its Close button (Firefox)
        if "window.close" in actions(win):
            act(win, "window.close")
        else:
            act([b for b in find(a.app, window=win, role="button", name="Close", exact=True, showing=False) if actions(b)][-1])
        print(json.dumps({"closed": title}, ensure_ascii=False))
    elif a.cmd == "windows":
        print("\n".join(w.get_name() or "" for w in windows(a.app)))
    elif a.cmd == "key":
        key(a.combo)
    elif a.cmd == "screenshot":
        w, h = desktop_size()
        print(json.dumps({"screenshot": screenshot(a.path), "size": [w, h], "note": "pixels in the picture are click-at coordinates"}))
    elif a.cmd == "click-at":
        print(json.dumps({"clicked": list(click_at(a.x, a.y, a.button, a.count))}))
    elif a.cmd == "type":
        type_text(a.string)
    else:
        sel = dict(role=a.role, name=a.name, window=a.window, exact=a.exact)
        if a.cmd == "find":
            print(json.dumps([describe(e) for e in find(a.app, showing=not a.all, **sel)], ensure_ascii=False, indent=1))
        else:
            el = one(a.app, **sel)
            if a.cmd == "act":
                print(json.dumps({"ok": act(el, a.action), "target": describe(el)}, ensure_ascii=False))
            elif a.cmd == "type-into":
                if a.from_file:
                    a.string = open(a.from_file).read()
                type_into(el, a.string)
                if a.enter:
                    key("enter")
                print(json.dumps({"typed": a.string[:80], "chars": len(a.string), "target": describe(el)}, ensure_ascii=False))
            elif a.cmd == "click":
                x, y = click(el, a.button, a.count)
                print(json.dumps({"clicked": [round(x), round(y)], "target": describe(el)}, ensure_ascii=False))
            elif a.cmd == "text":
                print(text(el))
            elif a.cmd == "set-text":
                print(json.dumps({"ok": set_text(el, a.value), "target": describe(el)}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (LookupError, KeyError, RuntimeError, NotImplementedError) as e:
        sys.exit(f"error: {e}")
