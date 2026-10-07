# GUI work as master: analyse, brief a coworker, check

For anything on screen that takes more than two or three actions, do not drive the GUI
yourself. Your context is large and every step would carry all of it. Hand the screen work to a
coworker (`delegate_task`) that starts with an empty memory and only your brief.

## 1. Analyse

- What exactly must be true at the end? Which values go where, what must be read back?
- Is a GUI needed at all? A file, a command or the app's settings is always first choice.
- Which app or page, and is it already open?
- `JARVIS_REPO/channels/gui/tool.py notes APP` prints what earlier checked runs learned about
  that app. Put the lines that matter into the brief.

## 2. Brief (everything the coworker needs, nothing else)

```
Goal: <one or two sentences, with the exact values to enter or read>
Where: <app, window or page; already open in the background session, do not reopen it>
Done means: <what is true on screen or on disk at the end>
Known about this app: <lines from the notes, if any>
Limits: GUI only. Never kill apps by name, no sudo, do not stop or restart the session.
        If blocked, stop and report where; do not retry the same thing for long.
Report: DONE or BLOCKED, what you read back, and the path of one final screenshot.
<then the whole text of COWORKER.md, word for word: the coworker does not have it>
```

One independent task per coworker. If a second step needs a value the first found, put that
value into the next brief; coworkers do not talk to each other.

## 3. Check, then answer

- Do not pass on "done" unchecked. Look at the coworker's final screenshot yourself
  (`vision_analyze`), or take one (`tool.py screenshot FILE`), or read the saved file. Check the
  thing that was asked for, value by value, not that "something happened".
- Blocked, and you see another way: brief one more coworker with what the first learned.
  Otherwise stop and say so plainly.
- After a result you checked: if the coworker had to discover something about the app that
  would have saved steps (where a setting lives, a dialog that always appears, a control that
  is not in the tree), record one line: `tool.py note APP "..."`. Facts about the app only,
  never the answer to a task.
- Answer the owner in a few lines: what was done and what shows it.
