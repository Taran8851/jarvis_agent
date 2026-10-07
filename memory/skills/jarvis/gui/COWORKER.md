# GUI coworker: how to operate the screen

You do one GUI task and report. Everything goes through one command, written `T` below:
`T=JARVIS_REPO/channels/gui/tool.py` (run it directly). It works in a hidden desktop session
that is already running; never stop or restart it, and never kill apps by name.

## The loop: look once, act in batches

```
$T apps                          # which apps are running
$T snap Firefox                  # numbered list of what can be acted on (ids like e12)
$T do 'type e12 "Asha"' 'type e14 "Verma"' 'press "button:Submit"' 'wait 1'
```

- `snap APP [--window "part of title"] [--grep word] [--all]` lists elements with their current
  values and states. `--all` includes what is scrolled out of view. Ids stay valid while the
  element stays.
- `do` runs several actions in one call. Before each one it checks that the target is still the
  same element and enabled; at the first failed check it stops and tells you. Its reply ends
  with **what changed** on screen (`+` new, `-` gone), so you do not need another `snap` to see
  the result. Read that, then send the next batch.
- **Put every step you are sure of into one `do`.** One action per call wastes your budget. A
  whole form, a menu path, or a run of key presses is one call.

## Actions for `do`

A target is an id (`e12`), a name (`"Save"`), or `role:name` (`"button:OK"`, `"menu item:Export..."`).
A name is looked up when its turn comes, so it can be something an earlier action opens.

```
press REF                 the element's own action (buttons, links, menu items, tick boxes, tabs)
type REF "text"           put text in a field
set REF 70                a slider or spinner
select REF                a row in a list
key ctrl+s                keys to the focused window; repeat with: key down x7
text "string"             type into whatever has focus
click REF [right] [double]            real pointer click on an element (when press does nothing)
click-at X Y [right] [double]         a pixel of a screenshot
hover X Y                 move the pointer there and leave it
drag X1 Y1 X2 Y2          press, move, release
wait 2                    seconds
screenshot /path/file.png
```

Key names: `ctrl+s`, `shift+f8`, `esc`, `enter`, `tab`, `menu`, `up/down/left/right`,
`home/end/pageup/pagedown`, `delete`, `f1`-`f12`.

Other commands: `$T windows APP`, `$T close APP --window "title"`, `$T launch APP [ARGS]` (only
if the app is not open), `$T notes APP` (what earlier runs learned about this app: read it first).

## When the list does not show it

Canvases, game boards, drawn widgets, VLC and slide text boxes are not in the tree. If `snap`
does not show what you need, do not keep searching: take **one** `screenshot`, look at it with
your image tool (ask it for pixel coordinates of what you need, all in one question), then act
with `click-at`, `drag`, `hover` and keys, again batched. Pixels in the picture are the
coordinates. Take another screenshot only to check a result the tree cannot show.

## Finish

1. Check the result once: the `changed` lines if they show it, else one screenshot, else the
   saved file. Look for a dialog left open or a document left unsaved.
2. Report in a few lines: `DONE` or `BLOCKED`, what you did, what you read back that shows it,
   and the path of your last screenshot if you took one. If blocked, say exactly where and why;
   do not keep retrying the same thing.
