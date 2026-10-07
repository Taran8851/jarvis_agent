#!/usr/bin/env bash
# Run FROM YOUR LAPTOP. Copies the Jarvis codebase to a server (~/jarvis) and runs setup there over SSH.
# Usage: bash setup/deploy.sh user@host [~/.ssh/id_ed25519] [port]
set -euo pipefail
TARGET="${1:?usage: deploy.sh user@host [keyfile] [port]}"
KEY="${2:-$HOME/.ssh/id_ed25519}"
PORT="${3:-22}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
[ -f "$HERE/.env" ] || { echo "fill setup/.env first (cp setup/.env.example setup/.env)"; exit 1; }
SSH=(ssh -t -i "$KEY" -p "$PORT" -o StrictHostKeyChecking=accept-new "$TARGET")   # -t: the Claude login prompt needs a terminal
"${SSH[@]}" 'mkdir -p ~/jarvis'
# Code dirs, plus the repo-root .env (router provider keys) when present. Both .env files are git-ignored.
parts=(core channels memory router setup)
[ -f "$ROOT/.env" ] && parts+=(.env)
(cd "$ROOT" && scp -i "$KEY" -P "$PORT" -q -r "${parts[@]}" "$TARGET:~/jarvis/")
"${SSH[@]}" 'chmod 600 ~/jarvis/setup/.env; [ -f ~/jarvis/.env ] && chmod 600 ~/jarvis/.env; bash -lc "bash ~/jarvis/setup/setup.sh"'
