#!/usr/bin/python3
"""One entry point for GUI control that picks the session: what Jarvis calls.

    python3 tool.py [--mode auto|bg|fg] [--owner-asked] launch CMD...   # start an app there
    python3 tool.py [--mode ...] GUI_ARGS...                            # any gui.py command
    python3 tool.py [--mode ...] snap APP | do 'ACTION' ...             # batch.py: numbered list, checked batches
    python3 tool.py notes APP | note APP "TEXT"                         # per-app notes (no session needed)
    python3 tool.py status

Background is the default: a wrong background choice fails a task (an app or a login is
missing there), a wrong foreground choice types into the owner's window. Foreground is allowed only
when their session is unlocked and has been idle for IDLE_FOR_FOREGROUND seconds, or when the owner
asked for it in their message (--owner-asked); gui.py then still checks before every key and
pointer action that the owner has not come back.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import bg  # noqa: E402

IDLE_FOR_FOREGROUND = 600
SYSTEM_PYTHON = "/usr/bin/python3"


def _real(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout


def idle_seconds():
    out = _real("gdbus", "call", "--session", "--dest", "org.gnome.Mutter.IdleMonitor", "--object-path",
                "/org/gnome/Mutter/IdleMonitor/Core", "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime")
    try:
        return int(out.split()[1].rstrip(",)")) / 1000
    except (IndexError, ValueError):
        return 0.0  # no answer from the real session: treat them as present


def locked():
    return "true" in _real("gdbus", "call", "--session", "--dest", "org.gnome.ScreenSaver", "--object-path",
                           "/org/gnome/ScreenSaver", "--method", "org.gnome.ScreenSaver.GetActive")


def choose_mode(requested="auto", owner_asked=False):
    """-> (mode, reason). Raises PermissionError when foreground is asked for and not safe."""
    if requested in ("auto", "bg"):
        return "bg", "background is the default"
    if locked():
        raise PermissionError("foreground refused: the screen is locked (keys would go to the lock screen); use --mode bg")
    idle = idle_seconds()
    if owner_asked:
        return "fg", "the owner asked for the foreground"
    if idle < IDLE_FOR_FOREGROUND:
        raise PermissionError(f"foreground refused: the owner was active {idle:.0f}s ago (needs {IDLE_FOR_FOREGROUND}s idle); "
                              "use --mode bg, or ask them")
    return "fg", f"their session has been idle for {idle:.0f}s"


def main():
    args, mode, owner_asked = sys.argv[1:], "auto", False
    while args and args[0].startswith("--"):
        if args[0] == "--mode":
            mode, args = args[1], args[2:]
        elif args[0] == "--owner-asked":
            owner_asked, args = True, args[1:]
        else:
            break
    if not args:
        sys.exit(__doc__)
    if args[0] == "status":
        print(json.dumps({"background": bg.status(), "real_idle_s": round(idle_seconds()), "real_locked": locked()}))
        return
    if args[0] in ("notes", "note"):   # files only: do not start a session for them
        os.execv(SYSTEM_PYTHON, [SYSTEM_PYTHON, str(HERE / "batch.py"), *args])
    try:
        mode, reason = choose_mode(mode, owner_asked)
    except PermissionError as e:
        sys.exit(f"error: {e}")
    print(f"[gui: {mode} — {reason}]", file=sys.stderr)

    if mode == "bg":
        try:
            bg.start()
        except (bg.LowMemory, bg.StartFailed) as e:
            sys.exit(f"error: {e}")
        env = bg.env()
    else:
        env = dict(os.environ)
    if args[0] == "launch":
        def running():
            out = subprocess.run([SYSTEM_PYTHON, str(HERE / "gui.py"), "apps"], capture_output=True, text=True, env=env).stdout
            return set(out.split("\n")) - {""}
        before, kinds = running(), sorted(bg.classify(args[1])["kinds"])
        if mode == "bg":
            bg.launch(args[1:])
        else:
            subprocess.Popen(args[1:], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        # Say whether the app can be driven: its name in the tree, or plainly that it has none.
        import time
        end = time.monotonic() + 25
        while time.monotonic() < end:
            new = running() - before
            if new:
                print(json.dumps({"launched": args[1], "kinds": kinds, "app": sorted(new)[0]}))
                return
            time.sleep(1)
        print(json.dumps({"launched": args[1], "kinds": kinds, "app": None,
                          "note": "no new app appeared in the accessibility tree within 25 s: it may already have been "
                                  "running (see `apps`), or it exposes no elements (Electron/Chromium apps need "
                                  "--force-renderer-accessibility; this was added automatically if kinds says electron). "
                                  "If it is running but unreadable, take one `screenshot` and use `click-at` and keys"}))
        return
    # the system interpreter, not whichever python called us: the Atspi bindings (python3-gi)
    # are a system package, and an agent's own virtualenv does not have them
    script = "batch.py" if args[0] in ("snap", "do") else "gui.py"
    os.execve(SYSTEM_PYTHON, [SYSTEM_PYTHON, str(HERE / script), *args], env)


if __name__ == "__main__":
    main()
