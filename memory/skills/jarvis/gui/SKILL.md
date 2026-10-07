---
name: gui
description: Operate desktop apps and websites on the laptop without screenshots.
version: 0.1.0
author: jarvis_agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [gui, desktop, firefox, automation, accessibility]
    category: software-development
    related_skills: [gnome-wayland-gui-automation]
---

# gui — drive apps through the accessibility tree

One command does everything: `JARVIS_REPO/channels/gui/tool.py` (run it directly; it needs the
system Python, not a virtualenv's). Below it is written `tool.py`. It reads and presses
named elements (buttons, links, fields, tabs), types, and as a last resort clicks. No screenshots unless the tree fails.

## Where it runs

- **Background (default).** A hidden GNOME session with its own apps and a logged-out Firefox.
  the owner sees nothing and can keep working; it also works while the screen is locked. You
  cannot see or touch their open windows from here.
- **Foreground (`--mode fg`).** His real session. Allowed only when the screen is unlocked and
  the owner has been idle 10 minutes, or the owner asked for it in their message (then add `--owner-asked`).
  If a call answers `paused: ... you are at the keyboard`, stop and tell them; do not retry.

Use the foreground only when the task needs their session: a window the owner has open, or a site
where only their Firefox is logged in.

## Commands

```
tool.py status                                        # background up? is the owner idle? locked?
tool.py launch firefox --new-window URL               # start an app in the chosen session
tool.py apps                                          # what is running there
tool.py windows Firefox                               # window titles
tool.py tree Firefox --window "part of title"         # the elements you can act on, one per line
tool.py act Firefox --role button --name "Send"       # press it (its own action; no pointer)
tool.py type-into Firefox --role entry --name "Search" "text" --enter
tool.py type-into code --role entry --name "main.c" --from-file /path/to/text   # code, multi-line
tool.py key ctrl+l                                    # a shortcut, into the focused window
tool.py click Firefox --name "Drawing canvas"         # real pointer click: canvas or last resort
tool.py find Firefox --role link --name "Next"        # details, incl. current text and states
tool.py close Firefox --window "part of title"        # close one window
tool.py screenshot [FILE.png]                         # fallback: one picture of the desktop
tool.py click-at X Y                                  # click a pixel of that picture
```

`tree` and `find` list what is on screen. Add `--all` for the whole page, including what is
scrolled out of view. `act`, `type-into` and `click` find off-screen elements by themselves and
scroll to them, so you never need to page down to reach a link.

## How to work

1. `tree` first, then act on a name you saw there. Do not guess names.
2. Prefer `act`. Use `type-into` for text (Firefox ignores other ways). `click` only when there
   is no action and no key for it.
3. `--name` matches exact, then starts-with, then contains. If several elements share a name,
   add `--window` or `--exact`, or check with `find`.
4. **A call that says ok is not proof.** Check, but cheaply:
   - During the task, do not read back after every step. Chain the steps you are sure of in one
     command and read the tree once at the end of the chain.
   - **Before you say done, take one screenshot and look at it**: is a dialog still open, is the
     document unsaved, is the result visibly there? That one picture is the check the owner trusts.
   - Take one more mid-task only at a point of no return (before Send, Submit, Save over a
     file) or when the tree does not show what you need. Never one per step.
   - For a saved file, one look at the file itself beats five: do not unzip and parse it repeatedly.
5. To open a URL, `launch firefox --new-window URL` (a new tab avoids the address bar being in
   a search engine's mode).
6. Stop the background session when a long job is done: `JARVIS_REPO/channels/gui/bg.py stop`.
7. Search the tree yourself: `tool.py tree Firefox | grep -i word`. A grep with no match exits 1;
   that is "not found", not a failure of the tool.

## When the tree does not show it: one screenshot

Some things are not in the tree: VLC's whole window, the text boxes on an Impress slide, a canvas,
a rating drawn as stars. When `apps`/`tree --all` do not show what you need, do not keep
searching the tree. Take **one** `tool.py screenshot`, look at the file with your image tool,
then act with `click-at X Y` (the picture's pixels are the coordinates) and keys. Take another
only after an action whose result you must see. Background session only. Go back to the tree as
soon as it has what you need: it is exact, a picture is a guess.

Key names for `key`: `ctrl+s`, `shift+f8`, `esc`, `enter`, `tab`, `menu` (context menu),
`up/down/left/right`, `home/end/pageup/pagedown`, `delete`, `f1`-`f12`.

## Launching apps

Always start apps with `tool.py launch APP [ARGS]`. It works out what kind of app it is and
starts it the right way (Electron apps such as VS Code, Obsidian and Spotify get accessibility
switched on and a data directory of their own; snaps and Firefox get what they need), then
prints the name to use, e.g. `{"launched": "code", "kinds": ["electron"], "app": "code"}`.
If `"app"` is null, the app showed no elements: say so, or try another route; do not guess.
An Electron app in the background is a fresh copy: Obsidian starts with no vault open.

## Writing code or long text

Put the text in a file and send it with `--from-file`: it is pasted, so indentation and line
breaks arrive exactly as written. Typing code line by line fights the editor's auto-indent.
In VS Code the editor is the `entry` named after the file; save with `tool.py key ctrl+s`.
A new file: launch VS Code on the path (`tool.py launch code ~/Jarvis/x.c`); file-chooser
dialogs are not available in the background session.

## Never

- **Never `pkill`, `killall` or `kill` a browser or app by name.** That kills the owner's own
  Firefox in their real session, with all their tabs. To close a window use `tool.py close`; to
  start clean, `JARVIS_REPO/channels/gui/bg.py stop` (it ends only the background session).
- Never type passwords or log the background Firefox into a site unless the owner said so for that site.

## Known limits

GTK popup-menu items have no names.
The background Firefox is logged out of every site. Canvas content can be clicked but not read.
