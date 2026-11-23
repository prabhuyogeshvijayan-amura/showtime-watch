#!/bin/zsh
# Stores Telegram + Gmail credentials as GitHub Secrets for the watcher, then sends a test alert.
# Run from this folder:  zsh setup-alerts.sh
set -e
REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner)
echo "Repo: $REPO"

echo "\n== Telegram =="
echo "1) In Telegram, open @BotFather, send /newbot, follow the prompts, copy the token."
echo "2) Open your new bot and send it any message (e.g. hi)."
read -s "TG_TOKEN?Paste bot token (hidden): "; echo
CHAT_ID=$(curl -s "https://api.telegram.org/bot${TG_TOKEN}/getUpdates" | python3 -c '
import sys, json
r = json.load(sys.stdin)
if not r.get("ok"): sys.exit("Telegram rejected the token: " + str(r.get("description")))
msgs = [u["message"] for u in r["result"] if "message" in u]
if not msgs: sys.exit("No messages found - send your bot a message first, then re-run.")
print(msgs[-1]["chat"]["id"])')
gh secret set TELEGRAM_BOT_TOKEN -R "$REPO" -b "$TG_TOKEN"
gh secret set TELEGRAM_CHAT_ID  -R "$REPO" -b "$CHAT_ID"
echo "Telegram saved (chat id $CHAT_ID)."

echo "\n== Gmail =="
echo "Create an App Password at https://myaccount.google.com/apppasswords (needs 2-Step Verification on)."
read "GM_USER?Gmail address to send from: "
read -s "GM_PASS?16-char App Password (hidden): "; echo
read "GM_TO?Send alerts to [$GM_USER]: "
GM_TO=${GM_TO:-$GM_USER}
gh secret set SMTP_USER -R "$REPO" -b "$GM_USER"
gh secret set SMTP_PASS -R "$REPO" -b "${GM_PASS// /}"
gh secret set EMAIL_TO  -R "$REPO" -b "$GM_TO"
echo "Email saved."

echo "\nSending a test alert from GitHub..."
gh workflow run watch.yml -R "$REPO" -f mode=test
echo "You should get a Telegram message and an email within ~1 minute."
echo "Run status: gh run list -R $REPO --limit 3"
