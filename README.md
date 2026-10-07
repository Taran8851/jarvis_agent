# jarvis_agent

A personal engineering agent you text or call on Telegram. It runs on your own machine, does
real work there (terminal, files, git), and remembers through a plain-markdown Obsidian vault.

Jarvis = [Hermes Agent](https://github.com/NousResearch/hermes-agent) (MIT: agent loop,
Telegram gateway, approvals, cron, skills) + this repo:

| Piece | Folder | What it adds |
|---|---|---|
| Identity | `core/` | `SOUL.md` (who Jarvis is) and memory seeds (who you are, your machine) |
| Work rules | `DEV.md` | Declare → wait → build → verify. Also the rules note of the vault |
| Memory | `memory/`, `vault-starter/` | The `brain` skill (how to read/write the vault), git sync, a starter vault |
| Voice calls | `channels/voice/` | Live Telegram calls both ways: Silero VAD → Whisper → Claude Haiku → Piper |
| Model router | `router/` | Optional LiteLLM proxy with cheap tiers for routine work |
| GUI control | `channels/gui/` | **Experimental**, GNOME on Wayland only: drives apps through the accessibility tree |

Status: this is one person's working setup, made generic. Setup is manual for now (fill a
file, run a script). It has been run on Ubuntu with systemd; nothing else is tested.

## Needs

- Linux with systemd user services. `loginctl enable-linger` if it should survive logout.
- A Claude Pro/Max plan logged into Claude Code (`MODEL_AUTH=claude`), or an Anthropic API key (`MODEL_AUTH=api`).
- A Telegram bot token (@BotFather) and your numeric Telegram id (@userinfobot).
- For voice calls (optional): a spare phone number with its own Telegram account for Jarvis, an
  `api_id`/`api_hash` from https://my.telegram.org, outbound UDP, ~3 GB disk (the CUDA runtime is
  most of it). Whisper uses an NVIDIA GPU when there is one and falls back to the CPU.

## Quickstart

```bash
git clone https://github.com/Taran8851/jarvis_agent.git && cd jarvis_agent
cp setup/.env.example setup/.env && chmod 600 setup/.env    # fill it in
$EDITOR core/memories/USER.md core/memories/MEMORY.md       # who you are, your machine
bash setup/setup.sh
```

`setup.sh` installs Hermes if missing, copies identity, memories and skills into `~/.hermes`,
merges your `.env`, creates or clones the vault, sets the model, and starts the Telegram
gateway as a user service. It is idempotent and backs up every file it overwrites as
`*.bak.<stamp>`. Then message your bot: "read _index.md and tell me the open decisions".

Voice calls, the router, a remote server, and troubleshooting: [SETUP.md](SETUP.md).

## Make it yours

| Change | Where |
|---|---|
| Who you are, your machine and its limits, your projects | `core/memories/USER.md`, `core/memories/MEMORY.md` |
| Jarvis's voice, when it may contact you, its hard limits | `core/SOUL.md` |
| How it works (the rules it follows and you hold it to) | `DEV.md` |
| Vault location, sync remote, model, optional parts | `setup/.env` |
| What the call model says and may do | `SYSTEM` in `channels/voice/brain.py` |
| Cheap model tiers and providers | `router/config.yaml` |

Re-run `bash setup/setup.sh` after changing anything under `core/`, `memory/` or `setup/.env`.

## Safety

- Only the first id in `TELEGRAM_ALLOWED_USERS` can call Jarvis; other callers are hung up on.
- Secrets live in `setup/.env` and `~/.hermes/.env` (mode 600), both outside git.
- The agent runs commands on your machine as you. Read Hermes' approval settings before
  giving it anything you can't afford to lose.
- Run it on **one** machine: two copies fight over the Telegram bot token and the call session.

## Layout

```
DEV.md             how to work — Jarvis's rules and this repo's
core/              SOUL.md, memories/ (templates: edit before setup)
channels/voice/    Telegram voice calls, service jarvis-voice; test_voice.py
channels/gui/      GUI control through AT-SPI (experimental)
memory/            vault-sync.sh (cron), skills/jarvis/{brain,gui}
vault-starter/     _index.md + note templates, used when you have no vault
router/            LiteLLM tiers, service jarvis-router
evals/phase1/      routing eval for the call model (75 cases)
setup/             setup.sh (this machine), deploy.sh (a server), .env.example
```

MIT licensed.
