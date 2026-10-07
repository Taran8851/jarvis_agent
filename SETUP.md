# SETUP.md — the parts after the quickstart

Written against Hermes Agent v0.21.5, py-tgcalls 3.0.0, Telethon 1.45.0 (2026-09/10). Re-check
against the versions you install.

## What setup.sh does

| Step | Result |
|---|---|
| Hermes | installs it with the official installer if `hermes` is not on PATH |
| Identity | `core/SOUL.md` → `~/.hermes/SOUL.md`; `core/memories/*` → `~/.hermes/memories/` |
| Skills | `memory/skills/jarvis/brain` → `~/.hermes/skills/jarvis/brain` (and `gui` when `JARVIS_GUI=1`) |
| Secrets | merges `setup/.env` into `~/.hermes/.env` (your non-empty values win, other keys untouched) |
| Vault | uses `OBSIDIAN_VAULT_PATH` if it exists; else clones `VAULT_GIT_URL`; else copies `vault-starter/` |
| Vault sync | only when the vault is a git checkout: `~/vault-sync.sh` in cron every 5 min |
| Config | `terminal.backend local`, `terminal.cwd $HOME`, `agent.max_turns 60`, memory on, cron catch-up |
| Model | `claude`: Claude Code CLI + the subscription plugin; `api`: the Anthropic provider |
| Voice | only when `TELEGRAM_CALL_API_ID/HASH` are set: venv, models, `jarvis-voice` service |
| Router | only when the repo-root `.env` has provider keys: `jarvis-router` service |
| Gateway | `hermes gateway install` as a user service (`--no-service` skips it) |

`MODEL_AUTH=claude` uses a Hermes plugin marked experimental; check that it fits your plan's terms.

## Voice calls

```
you (Telegram app) ──voice call──► Jarvis's own Telegram user account (py-tgcalls + Telethon)
   audio ─► Silero VAD endpointing ─► faster-whisper ─► persistent `claude -p --model haiku` ─► Piper ─► call
   model calls search_vault / read_note ─► vault_mcp.py (read-only, confined to OBSIDIAN_VAULT_PATH)
   model says [[ASK: task]] ─► `hermes chat --oneshot` ─► spoken if quick, else texted by the bot
   model says [[HANGUP]]    ─► call ends
Hermes ── `jarvis-call-me "reason"` ──► the service dials you
```

1. Make a Telegram account for Jarvis on a spare number, in the official app (Telegram blocks
   sign-ups from third-party clients). Get `api_id`/`api_hash` at https://my.telegram.org.
2. Put them in `setup/.env`, run `bash setup/setup.sh`.
3. Log the account in once (the code arrives in that account's Telegram app):
   ```
   set -a; . ~/.hermes/.env; set +a
   ~/.jarvis-voice/venv/bin/python channels/voice/tgcall.py login
   systemctl --user enable --now jarvis-voice
   ```
4. Message the Jarvis account once from your own Telegram so it can resolve and call you.

The voice path uses the Claude Code CLI (`claude`) for the call model whichever `MODEL_AUTH`
you chose, so it must be installed and logged in. Half-duplex: Jarvis doesn't listen while it
speaks. Speech model: `JARVIS_STT_MODEL` / `JARVIS_STT_DEVICE` in `~/.hermes/.env` (falls back
to CPU `base.en` if CUDA fails to load).

Tests: `cd channels/voice && ~/.jarvis-voice/venv/bin/python test_voice.py`.

## Model router (optional)

Put provider keys in a `.env` at the repo root (git-ignored): `GEMINI_API_KEY_1..4`,
`BEDROCK_MANTLE_KEY`. `setup.sh` then runs `router/install.sh`: LiteLLM on `127.0.0.1:4000`,
access key generated into `~/.jarvis-router/router.env`. `router/config.yaml` is one working
example; edit its `model_list` (and the key names `install.sh` copies) for your providers.

## GUI control (experimental)

GNOME on Wayland only. Needs the system Python's AT-SPI bindings (`python3-gi`,
`gir1.2-atspi-2.0`). `JARVIS_GUI=1` installs the skill; `channels/gui/tool.py` is the one entry
point. It was built and measured on a single machine: expect to debug it on yours.

## A remote server

`bash setup/deploy.sh user@host [keyfile] [port]` copies the code and your `.env` to `~/jarvis`
on the server and runs `setup.sh` there.

## Verify

| Check | Expect |
|---|---|
| `hermes gateway status` | running, Telegram connected |
| Bot: "read _index.md and tell me the open decisions" | an answer from the vault |
| `systemctl --user status jarvis-voice` | active; log line `jarvis-voice ready` |
| `jarvis-call-me "test"` | your Telegram rings; Jarvis says the reason |
| On the call: "bye" | goodbye, call ends |

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Call rings, stuck on "connecting"; log `TelegramServerError` | outbound UDP blocked | allow outbound UDP (firewall / cloud security group) |
| Redial says `CallBusy` | a failed call stays pending on Telegram's side | wait ~30 s |
| `hermes: command not found` over ssh | not on PATH in non-interactive shells | `~/.local/bin/hermes` |
| Bot gets `409 Conflict` | a second copy is polling the same bot token | stop the other host |
| vault-sync alerts "pull failed" but `git status` is clean | SSH to the git host times out | fix the SSH route (try `ssh.github.com:443` or port 22), then re-run |
| `hermes` fails with "Permission denied" | `~/.hermes` owned by another uid | move it aside and reinstall Hermes |
