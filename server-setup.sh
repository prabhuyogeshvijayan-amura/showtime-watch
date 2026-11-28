#!/usr/bin/env bash
# One-time setup of the showtime watcher on an Ubuntu server in India
# (e.g. AWS Lightsail, Mumbai). Paste this into the server's terminal:
#
#   sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/prabhuyogeshvijayan-amura/showtime-watch/main/server-setup.sh)"
#
# Re-running it later updates watch.py and optionally keeps your saved alert settings.
set -euo pipefail

RAW=https://raw.githubusercontent.com/prabhuyogeshvijayan-amura/showtime-watch/main
DIR=/opt/showtime-watch
ENVF=/etc/showtime-watch.env
SVC=showtime-watch
TEST_URL='https://www.district.in/movies/avengers-doomsday-movie-tickets-in-chennai-MV176735?frmtid=Mm5dpqTqJz&fromdate=2026-12-21'
UA='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36'

[ "$(id -u)" = 0 ] || { echo "Please run with sudo."; exit 1; }
command -v python3 >/dev/null || { apt-get update -qq && apt-get install -y -qq python3; }
ask()  { read -rp  "$1" "$2" </dev/tty; }
asks() { read -rsp "$1" "$2" </dev/tty; echo; }

echo "== 1/4  Checking that District allows this server =="
CODE=$(curl -s -o /dev/null -w '%{http_code}' -A "$UA" "$TEST_URL" || echo 000)
if [ "$CODE" != 200 ]; then
  echo "District returned HTTP $CODE - it blocks this server's network."
  echo "Stop here and delete this instance so you aren't billed for it."
  exit 1
fi
echo "OK - District answered HTTP 200."

echo "== 2/4  Installing the watcher =="
mkdir -p "$DIR"
curl -fsSL "$RAW/watch.py" -o "$DIR/watch.py"
id showtime >/dev/null 2>&1 || useradd --system --home-dir "$DIR" --shell /usr/sbin/nologin showtime
chown -R showtime: "$DIR"
sudo -u showtime python3 "$DIR/watch.py" --dry-run

echo "== 3/4  Alert settings (Telegram + Gmail) =="
TELEGRAM_BOT_TOKEN=""; TELEGRAM_CHAT_ID=""; SMTP_USER=""; SMTP_PASS=""; EMAIL_TO=""
# shellcheck disable=SC1090
if [ -f "$ENVF" ]; then . "$ENVF"; fi

# --- Telegram ---
DO_TG=y
if [ -n "$TELEGRAM_BOT_TOKEN" ]; then
  ask "Telegram alerts are set up. Change them? [y/N] " DO_TG; DO_TG=${DO_TG:-n}
fi
if [[ "$DO_TG" =~ ^[Yy] ]]; then
  # The running bot polls Telegram too; pause it so we can read your chat id.
  systemctl stop $SVC 2>/dev/null || true
  echo
  echo "Telegram: in the Telegram app open @BotFather, send /newbot, follow the prompts,"
  echo "copy the token it gives you, then open your new bot and send it any message (e.g. hi)."
  asks "Bot token (hidden): " TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID=$(curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates" | python3 -c '
import sys, json
r = json.load(sys.stdin)
if not r.get("ok"):
    sys.exit("Telegram rejected the token: %s" % r.get("description"))
msgs = [u["message"] for u in r["result"] if "message" in u]
if not msgs:
    sys.exit("No messages found - send your bot a message in Telegram, then re-run this script.")
print(msgs[-1]["chat"]["id"])')
  echo "Telegram chat id: $TELEGRAM_CHAT_ID"
fi

# --- Email ---
echo
if [ -n "$SMTP_USER" ]; then
  ask "Email alerts are on (to $EMAIL_TO). Change them? [y/N] " DO_EM
else
  ask "Add email alerts too? [y/N] " DO_EM
fi
DO_EM=${DO_EM:-n}
if [[ "$DO_EM" =~ ^[Yy] ]]; then
  echo "Gmail: create an App Password at https://myaccount.google.com/apppasswords"
  echo "(needs 2-Step Verification turned on). Leave the address blank to turn email off."
  ask "Gmail address to send from: " SMTP_USER
  SMTP_PASS=""; EMAIL_TO=""
  if [ -n "$SMTP_USER" ]; then
    asks "16-letter App Password (hidden): " SMTP_PASS
    SMTP_PASS=${SMTP_PASS// /}
    ask "Send alerts to [$SMTP_USER] (several? separate with commas): " EMAIL_TO
    EMAIL_TO=${EMAIL_TO:-$SMTP_USER}
    echo "Checking the Gmail login..."
    if SMTP_USER="$SMTP_USER" SMTP_PASS="$SMTP_PASS" python3 -c '
import os, smtplib, sys
try:
    s = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20)
    s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
    s.quit()
except Exception as e:
    sys.exit("Gmail said: %s" % (getattr(e, "smtp_error", b"").decode(errors="replace") or e))'; then
      echo "Gmail login OK."
    else
      echo "Gmail rejected that address/App Password. Email is left OFF (Telegram still works)."
      echo "Re-run 'sudo bash setup.sh' to try again."
      SMTP_USER=""; SMTP_PASS=""; EMAIL_TO=""
    fi
  fi
fi

umask 077
cat > "$ENVF" <<EOF
TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID="$TELEGRAM_CHAT_ID"
SMTP_USER="$SMTP_USER"
SMTP_PASS="$SMTP_PASS"
EMAIL_TO="$EMAIL_TO"
EOF
chmod 600 "$ENVF"

echo "== 4/4  Starting the always-on service =="
cat > /etc/systemd/system/$SVC.service <<EOF
[Unit]
Description=Showtime watcher (Doomsday + Dune IMAX @ Phoenix Market City)
After=network-online.target
Wants=network-online.target

[Service]
User=showtime
WorkingDirectory=$DIR
EnvironmentFile=$ENVF
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 $DIR/watch.py
Restart=always
RestartSec=60

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now $SVC
systemctl restart $SVC

echo
echo "Sending a test alert..."
( set -a; . "$ENVF"; set +a; python3 "$DIR/watch.py" --test-notify )

echo
echo "Done. The watcher checks Doomsday (English) and Dune (IMAX) every ~2 minutes and survives reboots."
echo "  Live log : sudo journalctl -u $SVC -f"
echo "  Status   : systemctl status $SVC"
echo "  Stop     : sudo systemctl disable --now $SVC"
