#!/usr/bin/python3
"""Numbered snapshots and checked action batches, on top of gui.py.

    batch.py snap APP [--window TITLE] [--all] [--grep WORD] [--max N] [--chrome]
    batch.py do [--app APP] [--window TITLE] ['ACTION' ...]      # or one action per line on stdin
    batch.py notes APP            # what earlier checked runs learned about this app
    batch.py note APP "TEXT"      # add one line (only after the result was checked)

Why: driving a GUI one action per model call cost 20-60 calls a task, half of them only to look
(evals/gui/stress/RESULTS.md). Here the model sees a short numbered list of what can be acted on,
sends several actions at once, and each is checked against the accessibility tree before it
runs: the target must still be there, be the same element, and be enabled. The first failed
check stops the batch, and the reply says what changed on screen. (UFO2's "speculative
multi-action", AgentOccam's reduced observation, Playwright's element refs:
docs/gui/oss-survey-2026-10-06.md.)

Actions (REF is an id from the last snapshot, e7, or a name: "Save", or role:name "button:OK";
a name is looked up when its turn comes, so it can be something an earlier action opens):
    press REF [ACTION]        the element's own action
    type REF "text"           focus the field, check it took focus, type
    set REF VALUE             a slider or spinner
    select REF                a row in a list
    click REF [right] [double]            a real pointer click on the element
    key COMBO [xN]            ctrl+s, enter, down x7
    text "string"             type into whatever has focus
    click-at X Y [right] [double]         a pixel of a screenshot
    hover X Y | hover REF     move the pointer there and leave it
    drag X1 Y1 X2 Y2          press, move in steps, release
    wait SECONDS
    screenshot [FILE]
"""
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gui  # noqa: E402
from gi.repository import Atspi  # noqa: E402  (gui has pinned the version)

STATE = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / f"jarvis-gui-snap-{'bg' if gui.BG else 'fg'}.json"
NOTES = HERE.parents[1] / "memory/skills/jarvis/gui/notes"
MAX_LINES = 150
# listed even without a name: a model has to know the field is there to fill it
INPUT_ROLES = {"entry", "password text", "spin button", "combo box", "check box", "radio button", "slider",
               "toggle button", "switch", "text"}
ROLES = INPUT_ROLES | {"button", "push button", "link", "menu", "menu item", "page tab", "list item", "table cell",
                       "tree item", "dialog", "frame", "heading", "label", "paragraph", "check menu item",
                       "radio menu item", "table row", "image"}
ECHO = {"label", "static", "text", "paragraph"}   # a button's own caption repeats its name


POPUPS = {"dialog", "alert", "menu", "popup menu", "file chooser", "tool tip"}


def _iter(top):
    """Depth-first with each element's path of child indices from `top`, and its zone: "doc"
    inside a web document, "popup" inside a dialog or menu, else None (window chrome)."""
    stack, n = [(top, (), None)], 0
    while stack and n < gui.MAX_NODES:
        el, path, zone = stack.pop()
        n += 1
        try:
            role = el.get_role_name()
        except Exception:
            continue
        if role == "document web":
            zone = "doc"
        elif role in POPUPS and zone is None:
            zone = "popup"
        yield el, path, zone
        try:
            count = min(el.get_child_count(), gui.MAX_KIDS)
        except Exception:
            continue
        kids = []
        for i in range(count):
            try:
                k = el.get_child_at_index(i)
            except Exception:
                k = None
            if k:
                kids.append((k, path + (i,), zone))
        stack.extend(reversed(kids))


def _interfaces(el):
    try:
        return el.get_interfaces()
    except Exception:
        return []


def _value(el, role, txt):
    """What a control currently holds, short: the model needs it to know a field is already right."""
    if role not in INPUT_ROLES:
        return None
    if "Value" in _interfaces(el) and role in ("slider", "spin button"):
        try:
            v = Atspi.Value.get_current_value(el)
            return str(int(v) if float(v).is_integer() else round(v, 2))
        except Exception:
            pass
    return txt[:60] or None


def _load():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {"app": None, "window": None, "els": [], "ids": {}, "next": 1}


def _real(acts):
    return [a for a in acts if a != "clickAncestor"]   # Gecko: "something above me is clickable"


def snapshot(app, window=None, showing=True, state=None, chrome=False):
    """-> (elements, state). One record per element worth showing: named and either actionable or
    carrying text, plus every input control. Ids stay the same from one snapshot to the next for
    an element that is still there (same window, role, name and position among its twins)."""
    old = state if state and state.get("app") == app else {"ids": {}, "next": 1}
    ids, nxt = dict(old.get("ids", {})), old.get("next", 1)
    wins = gui.windows(app)
    only = gui.window(app, window) if window else None
    els, seen, prev_name, hidden = [], {}, None, 0
    for wi, win in enumerate(wins):
        if only is not None and win != only:
            continue
        title = win.get_name() or ""
        rows = []
        for el, path, zone in _iter(win):
            try:
                role = el.get_role_name()
                names = gui.labels(el)
                name = names[0] if names else ""
                acts, txt = _real(gui.actions(el)), gui.text(el).replace("\ufffc", "").strip()
                # worth a line: an input, or something named that can be acted on or says something
                if not (role in INPUT_ROLES or (name and (acts or txt))) or role in ("document web", "frame"):
                    continue
                if showing and not gui.has(el, "showing"):
                    continue
                rows.append((el, path, zone, role, names, name, acts, txt))
            except Exception:
                continue
        # a browser window: the page and any dialog are what matters, not the toolbar buttons
        if not chrome and any(r[2] == "doc" for r in rows):
            hidden += sum(r[2] is None for r in rows)
            rows = [r for r in rows if r[2] is not None]
        for el, path, zone, role, names, name, acts, txt in rows:
            try:
                if role in ECHO and gui._norm(name) == prev_name:
                    continue
                prev_name = gui._norm(name)
                states = [s for s in ("focused", "selected", "checked", "expanded") if gui.has(el, s)]
                if not (gui.has(el, "enabled") or gui.has(el, "sensitive")):
                    states.append("disabled")
                twin = seen[(title, role, name)] = seen.get((title, role, name), 0) + 1
                ident = f"{title}|{role}|{name}|{twin}"
                if ident not in ids:
                    ids[ident] = f"e{nxt}"
                    nxt += 1
                desc = names[1] if len(names) > 1 and names[1] != name else ""
                els.append({"id": ids[ident], "role": role, "name": name, "desc": desc[:40], "value": _value(el, role, txt),
                            "states": states, "path": list(path), "wi": wi, "win": title, "el": el})
            except Exception:
                continue   # apps drop objects mid-walk
    new = {"app": app, "window": window, "ids": ids, "next": nxt, "titles": [w.get_name() or "" for w in wins], "hidden": hidden,
           "els": [{k: v for k, v in e.items() if k != "el"} for e in els]}
    return els, new


def line(e):
    s = f'{e["id"]} {e["role"]} "{e["name"][:80]}"'
    if e.get("desc"):
        s += f' ({e["desc"]})'
    if e.get("value") not in (None, ""):
        s += f' = "{e["value"]}"'
    if e["states"]:
        s += f' [{",".join(e["states"])}]'
    return s


def _sig(e):
    return (e["id"], e["role"], e["name"], e.get("value"), tuple(e["states"]))


class Stale(LookupError):
    """The target is not what the model saw: the batch must stop."""


def resolve(ref, app, state, wait=3.0, want=None):
    """REF -> a live element, or Stale. An id is followed back to the same place in the tree and
    must still be the same role and name there; failing that, the one element with that role and
    name in its window. A name is searched for, for up to `wait` seconds (a dialog may be opening)."""
    if re.fullmatch(r"e\d+", ref):
        rec = next((e for e in state.get("els", []) if e["id"] == ref), None)
        if rec is None:
            raise Stale(f"{ref} is not in the last snapshot of {app}; run snap again")
        wins = gui.windows(app)
        win = wins[rec["wi"]] if rec["wi"] < len(wins) and (wins[rec["wi"]].get_name() or "") == rec["win"] else \
            next((w for w in wins if (w.get_name() or "") == rec["win"]), wins[0] if len(wins) == 1 else None)
        if win is None:
            raise Stale(f'{ref}: its window "{rec["win"]}" is gone; windows now: {[w.get_name() for w in wins]}')
        el = win
        for i in rec["path"]:
            try:
                el = el.get_child_at_index(i)
            except Exception:
                el = None
            if el is None:
                break
        if el is not None:
            try:
                el.clear_cache()
                names = gui.labels(el)
                if el.get_role_name() == rec["role"] and (names[0] if names else "") == rec["name"]:
                    return el
            except Exception:
                pass
        hits = [h for h in gui.find(app, role=rec["role"], name=rec["name"], window=win, exact=True, showing=False)] \
            if rec["name"] else []
        if len(hits) == 1:
            return hits[0]
        raise Stale(f'{ref} was {rec["role"]} "{rec["name"]}": ' + ("not there any more" if not hits else "the screen changed and several match now"))
    role, name = None, ref
    m = re.match(r"^([a-z ]+):(.+)$", ref)
    if m and m.group(1) in ROLES:
        role, name = m.group(1), m.group(2)
    end = time.monotonic() + wait
    while True:
        try:
            first = gui.one(app, role=role, name=name)   # also scrolls an off-screen match into view
            if want is None or role:
                return first
            # several things carry a field's name (its form, its label); take the one the verb can use
            hits = gui.find(app, name=name) or gui.find(app, name=name, showing=False)
            return next((h for h in hits if want(h)), first)
        except LookupError:
            if time.monotonic() > end:
                raise Stale(f"nothing named {name!r}" + (f" with role {role}" if role else "") + f" in {app}")
            time.sleep(0.3)


def _ready(el, need_action=False):
    """The pre-flight check: on screen (scrolled to if not), enabled, and able to do what is asked."""
    if not gui.has(el, "showing"):
        try:
            gui.scroll_to(el)
            time.sleep(0.3)
        except Exception:
            pass   # GTK4 refuses scroll_to; its actions work off screen
    if not (gui.has(el, "enabled") or gui.has(el, "sensitive")):
        raise Stale(f'{el.get_role_name()} "{el.get_name()}" is disabled')
    if need_action and not gui.actions(el):
        raise Stale(f'{el.get_role_name()} "{el.get_name()}" has no action of its own: use click')


def _xy(args):
    return float(args[0]), float(args[1])


def _button_count(args):
    return ("right" if "right" in args else "middle" if "middle" in args else "left"), (2 if "double" in args else 1)


def one_action(verb, args, app, state):
    """Run one action. Returns a short note or None; raises to stop the batch."""
    is_input = lambda h: h.get_role_name() in INPUT_ROLES   # noqa: E731
    if verb == "press":
        el = resolve(args[0], app, state, want=lambda h: bool(_real(gui.actions(h))))
        _ready(el, need_action=True)
        gui.act(el, args[1] if len(args) > 1 else None)
    elif verb == "type":
        el = resolve(args[0], app, state, want=is_input)
        _ready(el)
        gui.type_into(el, args[1])
    elif verb == "set":
        el = resolve(args[0], app, state, want=is_input)
        _ready(el)
        gui.set_value(el, args[1])
    elif verb == "select":
        gui.select(resolve(args[0], app, state))
    elif verb == "click":
        el = resolve(args[0], app, state)
        _ready(el)
        button, count = _button_count(args[1:])
        x, y = gui.click(el, button, count)
        return f"at {round(x)},{round(y)}"
    elif verb == "key":
        times = int(args[1][1:]) if len(args) > 1 and re.fullmatch(r"x\d+", args[1]) else 1
        for _ in range(min(times, 200)):
            gui.key(args[0])
            time.sleep(0.03)
    elif verb == "text":
        gui.type_text(args[0])
    elif verb == "click-at":
        button, count = _button_count(args[2:])
        gui.click_at(*_xy(args), button, count)
    elif verb == "hover":
        x, y = _xy(args) if len(args) >= 2 and re.fullmatch(r"-?[\d.]+", args[0]) else gui.point(resolve(args[0], app, state))
        with gui.Pointer() as p:
            p.move(x, y)
            time.sleep(0.4)
        return f"at {round(x)},{round(y)}"
    elif verb == "drag":
        (x0, y0), (x1, y1) = _xy(args[:2]), _xy(args[2:4])
        with gui.Pointer() as p:
            p.move(x0, y0)
            p.press()
            for i in range(1, 13):   # drop targets need to see the motion
                p.move(x0 + (x1 - x0) * i / 12, y0 + (y1 - y0) * i / 12)
                time.sleep(0.02)
            time.sleep(0.1)
            p.release()
    elif verb == "wait":
        time.sleep(min(float(args[0]), 15))
    elif verb == "screenshot":
        return gui.screenshot(args[0] if args else None)
    else:
        raise ValueError(f"unknown action {verb!r}; see batch.py --help")
    return None


def show(els, grep=None, limit=MAX_LINES):
    rows = [line(e) for e in els if not grep or grep.lower() in line(e).lower()]
    out = rows[:limit]
    if len(rows) > limit:
        out.append(f"... {len(rows) - limit} more: narrow with --grep WORD or --window TITLE")
    return "\n".join(out)


def cmd_snap(argv):
    app, window, showing, grep, limit, chrome = None, None, True, None, MAX_LINES, False
    while argv:
        a = argv.pop(0)
        if a == "--chrome":
            chrome = True
        elif a == "--window":
            window = argv.pop(0)
        elif a == "--all":
            showing = False
        elif a == "--grep":
            grep = argv.pop(0)
        elif a == "--max":
            limit = int(argv.pop(0))
        else:
            app = a
    if not app:
        sys.exit("usage: snap APP [--window TITLE] [--all] [--grep WORD] [--max N] [--chrome]")
    els, state = snapshot(app, window, showing, _load(), chrome)
    STATE.write_text(json.dumps(state))
    print(f"{app}: windows: " + " | ".join(t or "(untitled)" for t in state["titles"]))
    if els:
        print(show(els, grep, limit))
        if state["hidden"]:
            print(f"({state['hidden']} browser toolbar elements not listed: --chrome shows them)")
    else:
        print("no elements: this window cannot be read through the tree. Take one screenshot and use click-at and keys.")


def cmd_do(argv):
    state = _load()
    app, window, raw = state.get("app"), state.get("window"), []
    while argv:
        a = argv.pop(0)
        if a == "--app":
            app = argv.pop(0)
        elif a == "--window":
            window = argv.pop(0)
        else:
            raw.append(a)
    if not raw:
        raw = sys.stdin.read().splitlines()
    steps = [s.strip() for chunk in raw for s in re.split(r"\n|\s;\s", chunk) if s.strip() and not s.strip().startswith("#")]
    if not steps:
        sys.exit("error: no actions given")

    def needs(s):
        t = s.split()
        return t[0] in ("press", "type", "set", "select", "click") or (t[0] == "hover" and len(t) > 1 and not re.fullmatch(r"-?[\d.]+", t[1]))
    if any(needs(s) for s in steps) and not app:
        sys.exit("error: no app: run snap APP first, or pass --app APP")
    if state.get("app") != app:
        state = {"app": app, "window": window, "els": [], "ids": {}, "next": 1}
    before = {}
    if app:
        try:
            before = {_sig(e): line(e) for e in snapshot(app, window, True, state)[0]}
        except LookupError:
            pass
        if any(s.split()[0] in ("key", "text") for s in steps):
            try:   # keys go to the focused window: make it this app's
                wins = gui.windows(app)
                gui.activate(gui.window(app, window) if window else wins[0])
            except Exception:
                pass
    stopped, n = False, 0
    for n, step in enumerate(steps, 1):
        try:
            toks = shlex.split(step)
            note = one_action(toks[0].lower(), toks[1:], app, state)
            print(f"{n} {step}: ok" + (f" ({note})" if note else ""))
        except Exception as e:   # anything unexpected stops the batch: the rest assumed it worked
            print(f"{n} {step}: STOPPED - {e}")
            stopped = True
            break
        time.sleep(0.15)
    if stopped and n < len(steps):
        print(f"not run: {len(steps) - n} action(s) after it")
    if not app:
        return
    time.sleep(0.5)   # let the app settle before reading what changed
    try:
        els, new = snapshot(app, window, True, state)
    except LookupError:
        print(f"{app} is no longer running; apps now: {sorted({a.get_name() for a in gui.apps() if a.get_child_count()})}")
        return
    STATE.write_text(json.dumps(new))
    after = {_sig(e): line(e) for e in els}
    added = [v for k, v in after.items() if k not in before]
    removed = [v for k, v in before.items() if k not in after]
    print("windows: " + " | ".join(t or "(untitled)" for t in new["titles"]))
    if not added and not removed:
        print("changed: nothing in the tree. If the result should be visible, take one screenshot.")
    else:
        print(f"changed: +{len(added)} -{len(removed)}")
        for v in added[:40]:
            print("+ " + v)
        for v in removed[:20]:
            print("- " + v.split(" ", 1)[1])
        if len(added) > 40 or len(removed) > 20:
            print("... more: run snap for the full list")


def _slug(app):
    return re.sub(r"[^a-z0-9]+", "-", app.lower()).strip("-") or "general"


def cmd_notes(argv):
    found = False
    for name in dict.fromkeys(("general", _slug(argv[0]) if argv else "general")):
        f = NOTES / f"{name}.md"
        if f.is_file():
            print(f.read_text().strip())
            found = True
    if not found:
        print("(no notes yet)")


def cmd_note(argv):
    if len(argv) < 2:
        sys.exit('usage: note APP "one line"')
    NOTES.mkdir(parents=True, exist_ok=True)
    f = NOTES / f"{_slug(argv[0])}.md"
    with open(f, "a") as fh:
        fh.write(f"- {' '.join(argv[1:]).strip()} ({time.strftime('%Y-%m-%d')}, added by Jarvis after its own check)\n")
    print(f"noted in {f}")


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        sys.exit(__doc__)
    cmd, rest = argv[0], argv[1:]
    {"snap": cmd_snap, "do": cmd_do, "notes": cmd_notes, "note": cmd_note}.get(cmd, lambda _: sys.exit(__doc__))(rest)


if __name__ == "__main__":
    try:
        main()
    except (LookupError, KeyError, RuntimeError, NotImplementedError, ValueError) as e:
        sys.exit(f"error: {e}")
