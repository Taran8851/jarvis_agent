#!/usr/bin/env bash
# Set up JARVIS on top of Hermes Agent, on this machine (laptop or server). Idempotent.
# Usage:  bash setup/setup.sh            (expects setup/.env next to this script)
#         bash setup/setup.sh --no-service   (skip systemd install)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"                      # repo root: core/ channels/ memory/ router/ setup/
HH="${HERMES_HOME:-$HOME/.hermes}"
STAMP="$(date +%Y%m%d-%H%M%S)"
NO_SERVICE=0; [ "${1:-}" = "--no-service" ] && NO_SERVICE=1
say(){ printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
bk(){ [ -e "$1" ] && { cp -a "$1" "$1.bak.$STAMP"; echo "    backed up $1 -> $1.bak.$STAMP"; } || true; }
cfg(){ hermes config set "$1" "$2" >/dev/null && echo "    $1 = $2" || echo "    WARN could not set $1 (run: hermes config set $1 $2)"; }

# 0. .env
[ -f "$HERE/.env" ] || { echo "setup/.env missing. cp setup/.env.example setup/.env and fill it."; exit 1; }
set -a
# shellcheck disable=SC1091  # .env is per-machine, not in the repo
. "$HERE/.env"
set +a
case "${MODEL_AUTH:-}" in claude|api) ;; *) echo "setup/.env: add MODEL_AUTH=claude (Claude Pro/Max login) or MODEL_AUTH=api (ANTHROPIC_API_KEY). See setup/.env.example."; exit 1;; esac
OBSIDIAN_VAULT_PATH="${OBSIDIAN_VAULT_PATH:-$HOME/jarvis-vault}"
OBSIDIAN_VAULT_PATH="$(eval echo "$OBSIDIAN_VAULT_PATH")"   # expand $HOME written in .env

# 1. Hermes
say "Hermes Agent"
if ! command -v hermes >/dev/null 2>&1; then
  echo "    not installed — running the official installer (clones source, bootstraps Python via uv)"
  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
  export PATH="$HH/bin:$HOME/.local/bin:$PATH"
fi
command -v hermes >/dev/null || { echo "hermes not on PATH after install; open a new shell and re-run"; exit 1; }
hermes --version 2>/dev/null || true
mkdir -p "$HH" "$HH/memories" "$HH/skills/jarvis"

# 2. Identity + memory seeds (backups kept)
say "SOUL.md, memories, brain skill"
bk "$HH/SOUL.md";              cp "$ROOT/core/SOUL.md" "$HH/SOUL.md"
bk "$HH/memories/USER.md";     cp "$ROOT/core/memories/USER.md" "$HH/memories/USER.md"
bk "$HH/memories/MEMORY.md";   cp "$ROOT/core/memories/MEMORY.md" "$HH/memories/MEMORY.md"
rm -rf "$HH/skills/jarvis/brain"; cp -r "$ROOT/memory/skills/jarvis/brain" "$HH/skills/jarvis/brain"
if [ "${JARVIS_GUI:-}" = 1 ]; then
  # experimental, GNOME on Wayland only; the skill files name the checkout as JARVIS_REPO
  rm -rf "$HH/skills/jarvis/gui"; cp -r "$ROOT/memory/skills/jarvis/gui" "$HH/skills/jarvis/gui"
  grep -rl JARVIS_REPO "$HH/skills/jarvis/gui" | while read -r f; do ROOT="$ROOT" perl -pi -e 's/JARVIS_REPO/$ENV{ROOT}/g' "$f"; done
  echo "    gui skill installed (experimental)"
fi

# 3. Merge env into ~/.hermes/.env (our non-empty values win; other keys untouched)
say "merging secrets into $HH/.env"
touch "$HH/.env"; bk "$HH/.env"
export OBSIDIAN_VAULT_PATH
KEYS="JARVIS_OWNER_NAME TELEGRAM_BOT_TOKEN TELEGRAM_ALLOWED_USERS TELEGRAM_HOME_CHANNEL OBSIDIAN_VAULT_PATH ANTHROPIC_API_KEY \
      TELEGRAM_CALL_API_ID TELEGRAM_CALL_API_HASH"
# awk reads values from ENVIRON, so |, & and \ in a secret pass through untouched (sed would mangle them)
( umask 077; KEYS="$KEYS" awk '
  BEGIN { n = split(ENVIRON["KEYS"], ks, " "); for (i = 1; i <= n; i++) if (ENVIRON[ks[i]] != "") want[ks[i]] = ENVIRON[ks[i]] }
  { k = $0; sub(/=.*/, "", k); if (k in want) { print k "=" want[k]; seen[k] = 1; next } print }
  END { for (k in want) if (!(k in seen)) print k "=" want[k] }
' "$HH/.env" > "$HH/.env.tmp" ) && mv "$HH/.env.tmp" "$HH/.env"
for k in $KEYS; do if [ -n "${!k:-}" ]; then echo "    $k set"; fi; done
chmod 600 "$HH/.env"

# 4. Vault
say "vault at $OBSIDIAN_VAULT_PATH"
if [ ! -d "$OBSIDIAN_VAULT_PATH" ]; then
  if [ -n "${VAULT_GIT_URL:-}" ]; then
    host="$(printf '%s' "$VAULT_GIT_URL" | sed -E 's#^[a-z]+://##; s#^[^@]*@##; s#[:/].*$##')"
    mkdir -p "$HOME/.ssh"; chmod 700 "$HOME/.ssh"; touch "$HOME/.ssh/config"; chmod 600 "$HOME/.ssh/config"
    scan_host="$host"; scan_port=22
    if [ "$host" = github.com ] && ! timeout 5 bash -c 'exec 3<>/dev/tcp/github.com/22' 2>/dev/null; then
      # port 22 blocked on this network: route github.com over ssh.github.com:443
      grep -q '^Host github.com$' "$HOME/.ssh/config" || printf 'Host github.com\n    HostName ssh.github.com\n    Port 443\n    User git\n' >> "$HOME/.ssh/config"
      scan_host=ssh.github.com; scan_port=443; echo "    port 22 blocked; ssh to github.com routed via ssh.github.com:443"
    fi
    ssh-keygen -F "[$scan_host]:$scan_port" -f "$HOME/.ssh/known_hosts" >/dev/null 2>&1 || ssh-keygen -F "$scan_host" -f "$HOME/.ssh/known_hosts" >/dev/null 2>&1 \
      || ssh-keyscan -p "$scan_port" -H "$scan_host" >> "$HOME/.ssh/known_hosts" 2>/dev/null || echo "    WARN could not fetch $scan_host host key"
    GIT_SSH_COMMAND="ssh -o ConnectTimeout=15 -o BatchMode=yes" git clone "$VAULT_GIT_URL" "$OBSIDIAN_VAULT_PATH" \
      || echo "    clone failed — is this server's public key added as a deploy key (write access) on the vault repo? (ssh-keygen -t ed25519; cat ~/.ssh/id_ed25519.pub)"
  else
    # no vault anywhere: start one from the templates, with DEV.md as its rules note
    cp -r "$ROOT/vault-starter" "$OBSIDIAN_VAULT_PATH"
    mkdir -p "$OBSIDIAN_VAULT_PATH/dev" && cp "$ROOT/DEV.md" "$OBSIDIAN_VAULT_PATH/dev/DEV.md"
    echo "    created a starter vault (open it in Obsidian; set VAULT_GIT_URL later to sync it)"
  fi
fi
if [ -d "$OBSIDIAN_VAULT_PATH/.git" ]; then
  cp "$ROOT/memory/vault-sync.sh" "$HOME/vault-sync.sh"; chmod +x "$HOME/vault-sync.sh"
  line="*/5 * * * * OBSIDIAN_VAULT_PATH=$OBSIDIAN_VAULT_PATH $HOME/vault-sync.sh >> $HOME/vault-sync.log 2>&1"
  # || true: grep -v exits 1 when no other lines remain; under pipefail that killed the subshell and wiped the crontab
  { crontab -l 2>/dev/null | grep -v vault-sync.sh || true; echo "$line"; } | crontab -
  echo "    cron: vault-sync every 5 min"
fi

# 5. Hermes config (only keys that exist in the shipped template)
say "hermes config"
cfg terminal.backend local
cfg terminal.cwd "$HOME"
cfg agent.max_turns 60
cfg memory.memory_enabled true
cfg memory.user_profile_enabled true
cfg cron.catch_up_missed true

# 6. Model: who pays for the tokens
say "model auth: ${MODEL_AUTH:-unset}"
case "${MODEL_AUTH:-}" in
  api)
    [ -n "${ANTHROPIC_API_KEY:-}" ] || { echo "    MODEL_AUTH=api but ANTHROPIC_API_KEY is empty"; exit 1; }
    cfg model.provider anthropic
    cfg model.default "${JARVIS_MODEL:-claude-sonnet-5}"
    ;;
  claude)
    # official native installer (no Node needed; apt's nodejs is too old on older Ubuntu). Installs to ~/.local/bin
    if ! command -v claude >/dev/null; then echo "    installing Claude Code CLI (native installer)"; curl -fsSL https://claude.ai/install.sh | bash; export PATH="$HOME/.local/bin:$PATH"; hash -r; fi
    command -v claude >/dev/null || { echo "    ERROR: claude still not on PATH after install. Run: curl -fsSL https://claude.ai/install.sh | bash"; exit 1; }
    echo "    claude $(claude --version 2>/dev/null | head -1)"
    if claude auth status >/dev/null 2>&1; then echo "    claude: already logged in"
    elif [ -t 0 ]; then echo "    claude: not logged in — follow the prompt (open the URL on your laptop, paste the code back here)"; claude auth login
    else echo "    WARN claude not logged in and no terminal: run 'claude auth login' on the server, then 'hermes gateway restart'"; fi
    hermes plugins install claude-subscription-directsdk >/dev/null 2>&1 || echo "    (plugin already installed or install failed: hermes plugins install claude-subscription-directsdk)"
    cfg model.provider claude-subscription-directsdk-experimental
    cfg model.default "${JARVIS_MODEL:-sonnet}"
    ;;
  *) echo "    ERROR: MODEL_AUTH must be 'claude' or 'api' in setup/.env (see setup/.env.example). Nothing runs without a model."; exit 1;;
esac
echo "    model: $(hermes config get model 2>/dev/null || echo '?')"

[ -n "${TELEGRAM_BOT_TOKEN:-}" ] || echo "    WARN TELEGRAM_BOT_TOKEN empty — the bot cannot start"

# 7b. Voice calls over Telegram (channels/voice): own venv, Piper voice, systemd user service
say "voice calls (Telegram)"
VH="$HOME/.jarvis-voice"
if [ -z "${TELEGRAM_CALL_API_ID:-}" ] || [ -z "${TELEGRAM_CALL_API_HASH:-}" ]; then
  echo "    skipped: set TELEGRAM_CALL_API_ID and TELEGRAM_CALL_API_HASH (my.telegram.org) in setup/.env"
else
  mkdir -p "$VH"; chmod 700 "$VH"
  UV="$(command -v uv || true)"
  for u in "$HH"/tools/uv-*/uv; do [ -n "$UV" ] || { [ -x "$u" ] && UV="$u"; }; done
  [ -n "$UV" ] || { echo "    ERROR: uv not found (Hermes ships one in $HH/tools)"; exit 1; }
  [ -x "$VH/venv/bin/python" ] || "$UV" venv -q --python 3.12 "$VH/venv"
  "$UV" pip install -q --python "$VH/venv/bin/python" -r "$ROOT/channels/voice/requirements.txt" && echo "    venv ok: $VH/venv"
  [ -f "$VH/en_US-lessac-medium.onnx" ] || (cd "$VH" && venv/bin/python -m piper.download_voices en_US-lessac-medium)
  [ -f "$VH/silero_vad.onnx" ] || curl -fsSL -o "$VH/silero_vad.onnx" https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx
  mkdir -p "$HOME/.local/bin"
  printf '#!/bin/sh\nexec %s %s call-me "$@"\n' "$VH/venv/bin/python" "$ROOT/channels/voice/tgcall.py" > "$HOME/.local/bin/jarvis-call-me"
  chmod +x "$HOME/.local/bin/jarvis-call-me"
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/jarvis-voice.service" <<UNIT
[Unit]
Description=Jarvis voice calls over Telegram
After=network-online.target

[Service]
EnvironmentFile=$HH/.env
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=LD_LIBRARY_PATH=$VH/venv/lib/python3.12/site-packages/nvidia/cublas/lib:$VH/venv/lib/python3.12/site-packages/nvidia/cudnn/lib
WorkingDirectory=$ROOT/channels/voice
ExecStart=$VH/venv/bin/python $ROOT/channels/voice/tgcall.py serve
Restart=on-failure
RestartSec=15

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  if [ -f "$VH/jarvis.session" ]; then
    systemctl --user enable --now jarvis-voice >/dev/null 2>&1 && systemctl --user restart jarvis-voice && echo "    jarvis-voice running"
  else
    echo "    Jarvis Telegram account not logged in yet. On the server run once:"
    echo "      set -a; . $HH/.env; set +a; $VH/venv/bin/python $ROOT/channels/voice/tgcall.py login"
    echo "    then: systemctl --user enable --now jarvis-voice"
  fi
fi

# 7c. Model router (router): LiteLLM on 127.0.0.1:4000, tiers over Gemini + Bedrock Mantle.
# Keys live in the repo-root .env (git-ignored); skipped when it has none.
say "model router"
if grep -qE '^(GEMINI_API_KEY_1|BEDROCK_MANTLE_KEY)=.' "$ROOT/.env" 2>/dev/null; then
  bash "$ROOT/router/install.sh"
else
  echo "    skipped: no GEMINI_API_KEY_1 / BEDROCK_MANTLE_KEY in $ROOT/.env"
fi

# 8. Gateway as a service
if [ $NO_SERVICE -eq 0 ]; then
  say "gateway service"
  command -v loginctl >/dev/null && loginctl enable-linger "$USER" 2>/dev/null || true   # user service survives logout
  hermes gateway install --force --start-now --start-on-login || echo "    service install failed — run: hermes gateway"
  sleep 3; hermes gateway status || true
fi

say "done. verify:"
cat <<EOT
  1. Telegram: message the bot "read _index.md and tell me the open decisions".
  2. Vault write: "log this session" → check $OBSIDIAN_VAULT_PATH/logs/$(date +%F).md, then git log there.
  3. Voice (if set up): jarvis-call-me "test" → your Telegram rings.
  4. Cost: hermes usage
  Logs: $HH/logs/gateway.log
EOT
