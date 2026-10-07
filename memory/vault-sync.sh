#!/usr/bin/env bash
# Two-way sync of the vault checkout with its git remote. Safe to run from cron.
# Pull with rebase (autostash keeps uncommitted agent writes), commit anything new, push.
# A failed pull aborts the rebase (never leave the checkout mid-rebase) and alerts Telegram.
set -euo pipefail
VAULT="${OBSIDIAN_VAULT_PATH:-$HOME/jarvis-vault}"
[ -d "$VAULT/.git" ] || { echo "vault-sync: $VAULT is not a git checkout"; exit 0; }
cd "$VAULT"

alert(){
  local env="${HERMES_HOME:-$HOME/.hermes}/.env" token chat
  token="$(grep -m1 '^TELEGRAM_BOT_TOKEN=' "$env" 2>/dev/null | cut -d= -f2-)" || true
  chat="$(grep -m1 '^TELEGRAM_HOME_CHANNEL=' "$env" 2>/dev/null | cut -d= -f2-)" || true
  [ -n "$token" ] && [ -n "$chat" ] || { echo "vault-sync: no Telegram token/chat in $env, alert not sent"; return 0; }
  curl -fsS -m 15 -o /dev/null "https://api.telegram.org/bot$token/sendMessage" \
    --data-urlencode "chat_id=$chat" --data-urlencode "text=$1" || echo "vault-sync: Telegram alert failed"
}

git add -A
if ! git diff --cached --quiet; then
  git -c user.name="jarvis" -c user.email="jarvis@localhost" commit -q -m "jarvis: $(date -u +%Y-%m-%dT%H:%MZ)"
fi
if ! git pull --rebase --autostash -q; then
  if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ]; then git rebase --abort; fi
  msg="vault-sync: pull failed on $(hostname) (conflict or auth?). Rebase aborted, local commits kept. Resolve by hand in $VAULT."
  echo "$msg"; alert "$msg"; exit 1
fi
git push -q
