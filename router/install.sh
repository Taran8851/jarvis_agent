#!/usr/bin/env bash
# Install/refresh the Jarvis model router (LiteLLM proxy on 127.0.0.1:4000) as the user service
# jarvis-router. Idempotent. Provider keys come from the repo-root .env (git-ignored):
#   GEMINI_API_KEY_1..4, BEDROCK_MANTLE_KEY
# A local access key (JARVIS_ROUTER_KEY) is generated once and kept in ~/.jarvis-router/router.env.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ENV="$(cd "$HERE/.." && pwd)/.env"
R="$HOME/.jarvis-router"
mkdir -p "$R"; chmod 700 "$R"

UV="$(command -v uv || true)"
for u in "$HOME"/.hermes/tools/uv-*/uv; do [ -n "$UV" ] || { [ -x "$u" ] && UV="$u"; }; done
[ -n "$UV" ] || { echo "uv not found"; exit 1; }
[ -x "$R/venv/bin/python" ] || "$UV" venv -q --python 3.12 "$R/venv"
"$UV" pip install -q --python "$R/venv/bin/python" -r "$HERE/requirements.txt"
echo "    router venv ok: $("$R/venv/bin/python" -c 'import importlib.metadata as m; print("litellm", m.version("litellm"))')"

[ -f "$REPO_ENV" ] || { echo "    ERROR: $REPO_ENV missing (provider keys)"; exit 1; }
old_key="$(grep -m1 '^JARVIS_ROUTER_KEY=' "$R/router.env" 2>/dev/null | cut -d= -f2- || true)"
( umask 077
  grep -E '^(GEMINI_API_KEY_[1-4]|BEDROCK_MANTLE_KEY)=' "$REPO_ENV"
  echo "JARVIS_ROUTER_KEY=${old_key:-sk-jarvis-$(python3 -c 'import secrets; print(secrets.token_hex(24))')}"
) > "$R/router.env.tmp" && mv "$R/router.env.tmp" "$R/router.env"
echo "    keys: $(grep -c '=' "$R/router.env") entries in $R/router.env (600)"

mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/jarvis-router.service" <<UNIT
[Unit]
Description=Jarvis model router (LiteLLM proxy, 127.0.0.1:4000)
After=network-online.target

[Service]
EnvironmentFile=$R/router.env
ExecStart=$R/venv/bin/litellm --config $HERE/config.yaml --host 127.0.0.1 --port 4000
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now jarvis-router >/dev/null 2>&1
systemctl --user restart jarvis-router
for _ in $(seq 30); do curl -s -m 2 -o /dev/null http://127.0.0.1:4000/health/liveliness && break; sleep 2; done
if curl -s -m 2 -o /dev/null http://127.0.0.1:4000/health/liveliness; then echo "    jarvis-router up on 127.0.0.1:4000"
else echo "    ERROR: router not answering; journalctl --user -u jarvis-router"; exit 1; fi
