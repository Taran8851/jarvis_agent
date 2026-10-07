#!/usr/bin/python3
"""The background session: a second, invisible GNOME Shell for Jarvis to work in while the owner
uses the real one. Its own session bus, accessibility bus, settings database and Wayland
display; no physical input device reaches it and nothing it does reaches the real seat.

    python3 bg.py start | stop | status | env
    python3 bg.py run CMD...        # run CMD inside the session (e.g. an eval script)
    python3 bg.py classify APP      # what kind of app it is and so how it gets launched

Everything lives in one systemd slice (jarvis-bg.slice), so stopping the slice stops the Shell,
its apps and its helper daemons together. Design: docs/gui/background-mode-design.pdf.
Stdlib only.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

UNIT, SLICE = "jarvis-bg.service", "jarvis-bg.slice"
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
RD = RUNTIME / "jarvis-bg-run"          # generated config for the session
# Firefox is a snap, and its sandbox only reaches a Wayland socket named wayland-<n> in the
# runtime dir, a session bus on an abstract @/tmp/dbus-* socket (or the real one), and an
# accessibility socket under <runtime>/at-spi/. The three names below are chosen to fit.
DISPLAY_NAME = "wayland-9"
BUS = "unix:abstract=/tmp/dbus-jarvisbg"
A11Y_SOCKET = RUNTIME / "at-spi" / "bus_jarvis"   # see a11y_bus.py for why this name
MONITOR = "1600x900"
# A profile of its own: Firefox will not open one profile twice, and Jarvis reading untrusted
# pages should not hold the user's cookies and saved passwords. Starts logged out everywhere.
FIREFOX_PROFILE = Path.home() / "snap/firefox/common/.mozilla/firefox/jarvis-bg"
FIREFOX_PREFS = """user_pref("browser.aboutwelcome.enabled", false);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("datareporting.policy.dataSubmissionPolicyBypassNotification", true);
user_pref("browser.tabs.warnOnClose", false);
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("media.autoplay.default", 5);
user_pref("toolkit.startup.max_resumed_crashes", -1);
user_pref("media.volume_scale", "0.0");
"""
# volume_scale: background work must never sound on the real speakers, whatever the audio
# server does with the stream. max_resumed_crashes: stopping the session kills Firefox, and after a few such "crashes" it
# opens a "Troubleshoot Mode?" dialog instead of a window.
MIN_FREE_MB = 3000                      # refuse to start below this (the slice is capped at 3 GB)
SERVICES = Path("/usr/share/dbus-1/services")

# Only these may be started on demand on the hidden bus. Anything else an app asks for
# (evolution, the file indexer, the keyring) fails to activate and the app carries on; with
# the stock service directories a second copy of each started against the same home.
ALLOWED = ["ca.desrt.dconf", "org.freedesktop.portal.Desktop", "org.freedesktop.impl.portal.desktop.gnome"]

DCONF = """[org/gnome/shell]
enabled-extensions=['computer-use-linux@avifenesh.dev']
disable-user-extensions=false

[org/gnome/desktop/lockdown]
disable-lock-screen=true

[org/gnome/desktop/screensaver]
lock-enabled=false

[org/gnome/desktop/session]
idle-delay=uint32 0

[org/gnome/desktop/interface]
enable-animations=false
enable-hot-corners=false
toolkit-accessibility=true

[org/gnome/desktop/input-sources]
sources=[('xkb', 'us')]
"""
# disable-lock-screen matters: the hidden Shell hears the login session's Lock signal and,
# without it, locks itself when the real screen locks (extensions unload, keys go nowhere).


class LowMemory(RuntimeError):
    pass


class StartFailed(RuntimeError):
    pass


def env():
    """What a process needs to live in the background session."""
    e = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "XAUTHORITY", "AT_SPI_BUS_ADDRESS")}
    e.update(DBUS_SESSION_BUS_ADDRESS=BUS, WAYLAND_DISPLAY=DISPLAY_NAME, DCONF_PROFILE=str(RD / "dconf-profile"),
             GDK_BACKEND="wayland", XDG_SESSION_TYPE="wayland", JARVIS_GUI_SESSION="bg",
             GNOME_ACCESSIBILITY="1", MOZ_ENABLE_WAYLAND="1",
             # Qt: stay off X (it prefers xcb on GNOME) and expose the tree without a screen reader
             QT_QPA_PLATFORM="wayland", QT_LINUX_ACCESSIBILITY_ALWAYS_ON="1",
             PULSE_SERVER="unix:/nonexistent/jarvis-bg-silent", PIPEWIRE_REMOTE="jarvis-bg-silent", PATH=f"{RD}/bin:{os.environ.get('PATH', '')}")
    return e


def _session_vars():
    return {k: env()[k] for k in ("DBUS_SESSION_BUS_ADDRESS", "WAYLAND_DISPLAY", "DCONF_PROFILE", "GDK_BACKEND",
                                  "XDG_SESSION_TYPE", "JARVIS_GUI_SESSION", "GNOME_ACCESSIBILITY", "MOZ_ENABLE_WAYLAND", "QT_QPA_PLATFORM",
                                  "QT_LINUX_ACCESSIBILITY_ALWAYS_ON", "PULSE_SERVER", "PIPEWIRE_REMOTE", "PATH")}


def _sc(*args):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def _free_mb():
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return 0


def healthy():
    """Unit active and the Shell answering on its bus. A pid alone is not health: a Shell that
    failed to create its Wayland socket is still a pid."""
    if _sc("is-active", UNIT).stdout.strip() != "active":
        return False
    out = subprocess.run(["busctl", "--user", "get-property", "org.gnome.Mutter.RemoteDesktop",
                          "/org/gnome/Mutter/RemoteDesktop", "org.gnome.Mutter.RemoteDesktop", "Version"],
                         capture_output=True, text=True, env=env(), timeout=5)
    return out.returncode == 0


def _write_config():
    svc = RD / "services"
    svc.mkdir(parents=True, exist_ok=True)
    for name in ALLOWED:
        text = (SERVICES / f"{name}.service").read_text()
        (svc / f"{name}.service").write_text("\n".join(l for l in text.splitlines() if not l.startswith("SystemdService=")) + "\n")
    (RD / "bus.conf").write_text(f"""<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <keep_umask/>
  <listen>{BUS}</listen>
  <auth>EXTERNAL</auth>
  <servicedir>{svc}</servicedir>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
""")
    # the database name becomes part of a D-Bus object path, so no hyphen in it
    (RD / "dconf-profile").write_text("user-db:jarvisbg\n")
    (RD / "dconf.ini").write_text(DCONF)
    FIREFOX_PROFILE.mkdir(parents=True, exist_ok=True)
    (FIREFOX_PROFILE / "user.js").write_text(FIREFOX_PREFS)
    # One shim, for the browser only: scripts and other apps start `firefox` by name (opening
    # a link, an eval's subprocess call) without going through launch(). Everything else gets
    # its recipe in command(), by what kind of app it is, not by a wrapper per app.
    (RD / "bin").mkdir(exist_ok=True)
    for stale in (RD / "bin").iterdir():
        stale.unlink()
    shim = RD / "bin" / "firefox"
    shim.write_text(f'#!/bin/sh\nexec /usr/bin/python3 "{Path(__file__).resolve()}" exec firefox "$@"\n')
    shim.chmod(0o755)
    start = RD / "start.sh"
    start.write_text(f"""#!/bin/sh
rm -f "{RUNTIME}/{DISPLAY_NAME}" "{RUNTIME}/{DISPLAY_NAME}.lock"
unset DISPLAY WAYLAND_DISPLAY XAUTHORITY AT_SPI_BUS_ADDRESS
export DBUS_SESSION_BUS_ADDRESS="{BUS}" DCONF_PROFILE="{RD}/dconf-profile" XDG_SESSION_TYPE=wayland
dbus-daemon --config-file="{RD}/bus.conf" --nofork --nopidfile &
i=0; while ! dbus-send --session --dest=org.freedesktop.DBus --print-reply / org.freedesktop.DBus.GetId >/dev/null 2>&1 && [ $i -lt 50 ]; do sleep 0.1; i=$((i+1)); done
dconf load / < "{RD}/dconf.ini"
mkdir -p "{A11Y_SOCKET.parent}"; rm -f "{A11Y_SOCKET}"
/usr/bin/python3 "{Path(__file__).resolve().parent / 'a11y_bus.py'}" "{A11Y_SOCKET}" &
exec gnome-shell --headless --wayland --no-x11 --wayland-display {DISPLAY_NAME} --virtual-monitor {MONITOR}
""")
    start.chmod(0o755)
    return start


def start(min_free_mb=MIN_FREE_MB, timeout=20):
    """Start the session if it is not already healthy. Idempotent."""
    if healthy():
        return env()
    stop()  # a unit that is active but not healthy, or leftovers of a crashed one
    free = _free_mb()
    if free < min_free_mb:
        raise LowMemory(f"{free} MB available, {min_free_mb} MB needed to start the background session")
    script = _write_config()
    run = subprocess.run(["systemd-run", "--user", f"--unit={UNIT}", f"--slice={SLICE}", "-p", "MemoryMax=3G",
                          "-p", "MemoryHigh=2500M", "--collect", "-q", str(script)], capture_output=True, text=True)
    if run.returncode:
        raise StartFailed(run.stderr.strip())
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if healthy():
            return env()
        time.sleep(0.5)
    log = subprocess.run(["journalctl", "--user", "-u", UNIT, "-n", "8", "--no-pager", "-o", "cat"],
                         capture_output=True, text=True).stdout
    stop()
    raise StartFailed(f"background session not healthy after {timeout}s:\n{log}")


def stop():
    _sc("stop", UNIT)
    _sc("stop", SLICE)
    _sc("reset-failed", UNIT)
    for f in (RUNTIME / DISPLAY_NAME, RUNTIME / f"{DISPLAY_NAME}.lock", A11Y_SOCKET):
        f.unlink(missing_ok=True)


def _snap_app(name):
    """(command, command-chain) of a snap's app from its snap.yaml, or None if `name` is not a
    snap app. A few lines of YAML read by hand: stdlib has no YAML parser."""
    yaml = Path(f"/snap/{name}/current/meta/snap.yaml")
    if not yaml.is_file():
        return None
    command, chain, in_app, in_chain = None, [], False, False
    for line in yaml.read_text().splitlines():
        if line.rstrip() == f"  {name}:":
            in_app = True
        elif in_app and line.startswith("  ") and not line.startswith("   "):
            break  # next app
        elif in_app and line.startswith("    command:"):
            command = line.split(":", 1)[1].strip()
        elif in_app:
            in_chain = line.strip() == "command-chain:" or (in_chain and line.startswith("    - "))
            if in_chain and line.startswith("    - "):
                chain.append(line.strip()[2:])
    return (command, chain) if command else None


def classify(name):
    """What kind of app a command is, read off the disk: which launch recipe it needs."""
    path = shutil.which(name, path=os.environ.get("PATH", "").replace(f"{RD}/bin:", "")) or name
    real, kinds = os.path.realpath(path), set()
    snap = os.path.basename(path) if real == "/usr/bin/snap" else None
    if not snap and os.path.isfile(path) and os.path.getsize(path) < 4096:
        try:   # Ubuntu's /usr/bin/firefox is a script that hands over to the snap
            snap = os.path.basename(path) if f"/snap/bin/{os.path.basename(path)}" in open(path, errors="replace").read() else None
        except OSError:
            pass
    if snap and _snap_app(snap):
        kinds.add("snap")
    # look only in the app's own directory: from a shared one (/usr/bin, /usr) the search
    # would find some other app's Electron files and call everything Electron
    shared = {"/", "/usr", "/usr/bin", "/bin", "/usr/local", "/usr/local/bin", "/usr/lib", "/usr/share", "/opt", str(Path.home())}
    roots = [Path(f"/snap/{snap}/current")] if "snap" in kinds else \
        [d for d in (Path(real).parent, Path(real).parent.parent) if str(d) not in shared]
    for root in roots:   # Electron: an app.asar or Chromium's resource packs sit beside the binary
        for d, sub, files in os.walk(root):
            if d[len(str(root)):].count(os.sep) >= 3:
                sub[:] = []
            if "app.asar" in files or "chrome_100_percent.pak" in files:
                kinds.add("electron")
                break
    if os.path.basename(path) == "firefox":
        kinds.add("firefox")
    if os.path.basename(path) in ("soffice", "libreoffice", "localc", "lowriter", "loimpress", "lodraw"):
        kinds.add("libreoffice")
    return {"name": os.path.basename(path), "path": path, "kinds": kinds, "snap": snap if "snap" in kinds else None}


def command(argv):
    """argv rewritten for the background session by kind of app -> (argv, kinds).

    electron: no accessibility tree unless forced; and with the default data dir a second
              instance just hands its arguments to the user's running one in the real session.
    firefox:  will not open one profile twice.
    libreoffice: one instance per profile; with the default one a document would open in the
              user's running LibreOffice, and crash recovery would be shared.
    snap:     `snap run` asks systemd for a scope over the session bus, and the hidden bus has
              no systemd, so enter the snap on the real bus and switch to the hidden one inside.
    """
    info = classify(argv[0])
    name, kinds, args = info["name"], info["kinds"], list(argv[1:])
    if "electron" in kinds:
        data = Path.home() / (f"snap/{name}/common/jarvis-bg" if "snap" in kinds else f".config/jarvis-bg/{name}")
        args = ["--force-renderer-accessibility", "--ozone-platform=wayland", f"--user-data-dir={data}",
                "--disable-workspace-trust", *args]
    if "firefox" in kinds:
        args = ["--profile", str(FIREFOX_PROFILE), *args]
    if "libreoffice" in kinds:
        args = [f"-env:UserInstallation=file://{Path.home()}/.config/jarvis-bg/libreoffice", "--norestore", *args]
    if "snap" in kinds:
        cmd, chain = _snap_app(name)
        inner = " ".join(f'"$SNAP/{c}"' for c in (*chain, cmd))
        script = (f'export DBUS_SESSION_BUS_ADDRESS="$JARVIS_BG_BUS" AT_SPI_BUS_ADDRESS="unix:path={A11Y_SOCKET}" '
                  f'PULSE_SERVER="unix:/nonexistent/jarvis-bg-silent"; exec {inner} "$@"')
        return (["env", f"JARVIS_BG_BUS={BUS}", f"DBUS_SESSION_BUS_ADDRESS=unix:path={RUNTIME}/bus",
                 "snap", "run", "--shell", name, "-c", script, name, *args], kinds)
    return [info["path"], *args], kinds


def launch(argv):
    """Start an app inside the session, in the slice. Returns the transient unit's name."""
    argv, _ = command(argv)
    argv[0] = shutil.which(argv[0]) or argv[0]   # systemd wants a path, not a name
    unit = f"jarvis-bg-app-{int(time.time() * 1000)}.service"
    sets = [f"--setenv={k}={v}" for k, v in _session_vars().items()]
    # RemainAfterExit: launchers like `code` fork and exit, and systemd would otherwise kill
    # what they started as soon as the launcher is gone.
    # UnsetEnvironment: the unit inherits the user manager's DISPLAY, which is the real screen's
    # Xwayland; an X11 app would open its window in front of the owner.
    run = subprocess.run(["systemd-run", "--user", f"--unit={unit}", f"--slice={SLICE}", "--collect", "-q",
                          "-p", "RemainAfterExit=yes",
                          "-p", "UnsetEnvironment=DISPLAY XAUTHORITY AT_SPI_BUS_ADDRESS", *sets, *argv],
                         capture_output=True, text=True)
    if run.returncode:
        raise RuntimeError(f"launch failed: {run.stderr.strip()}")
    return unit


def status():
    active = _sc("is-active", UNIT).stdout.strip() == "active"
    info = {"active": active, "healthy": healthy() if active else False}
    if active:
        mem = _sc("show", SLICE, "-p", "MemoryCurrent", "--value").stdout.strip()
        info["memory_mb"] = int(mem) // 2**20 if mem.isdigit() else None
        since = _sc("show", UNIT, "-p", "ActiveEnterTimestampMonotonic", "--value").stdout.strip()
        if since.isdigit():
            info["uptime_s"] = int(time.clock_gettime(time.CLOCK_MONOTONIC) - int(since) / 1e6)
        wins = subprocess.run(["busctl", "--user", "--json=short", "call", "dev.avifenesh.ComputerUseLinux.WindowControl",
                               "/dev/avifenesh/ComputerUseLinux/WindowControl", "dev.avifenesh.ComputerUseLinux.WindowControl",
                               "ListWindows"], capture_output=True, text=True, env=env(), timeout=5)
        try:
            info["windows"] = [w["title"] for w in json.loads(json.loads(wins.stdout)["data"][0])]
        except (ValueError, KeyError, IndexError):
            info["windows"] = None  # extension not loaded in the hidden Shell
    return info


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "start":
        start()
        print(json.dumps(status()))
    elif cmd == "stop":
        stop()
    elif cmd == "status":
        print(json.dumps(status()))
    elif cmd == "env":
        print("\n".join(f"{k}={v}" for k, v in _session_vars().items()))
    elif cmd == "run":
        start()
        os.execvpe(sys.argv[2], sys.argv[2:], env())
    elif cmd == "exec":       # one app, with its recipe, in place (what the firefox shim calls)
        argv, _ = command(sys.argv[2:])
        os.execvpe(argv[0], argv, env())
    elif cmd == "classify":
        info = classify(sys.argv[2])
        print(json.dumps({**info, "kinds": sorted(info["kinds"])}))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    try:
        main()
    except (LowMemory, StartFailed) as e:
        sys.exit(f"error: {e}")
