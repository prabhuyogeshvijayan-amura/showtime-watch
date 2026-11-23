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
KEEP=n
if [ -f "$ENVF" ]; then ask "Saved alert settings found. Keep them? [Y/n] " KEEP; KEEP=${KEEP:-y}; fi
if [[ ! "$KEEP" =~ ^[Yy] ]]; then
  # The running bot polls Telegram too; pause it so we can read your chat id.
  systemctl stop $SVC 2>/dev/null || true
  echo
  echo "Telegram: in the Telegram app open @BotFather, send /newbot, follow the prompts,"
  echo "copy the token it gives you, then open your new bot and send it any message (e.g. hi)."
  asks "Bot token (hidden): " TG_TOKEN
  CHAT_ID=$(curl -s "https://api.telegram.org/bot${TG_TOKEN}/getUpdates" | python3 -c '
import sys, json
r = json.load(sys.stdin)
if not r.get("ok"):
    sys.exit("Telegram rejected the token: %s" % r.get("description"))
msgs = [u["message"] for u in r["result"] if "message" in u]
if not msgs:
    sys.exit("No messages found - send your bot a message in Telegram, then re-run this script.")
print(msgs[-1]["chat"]["id"])')
  echo "Telegram chat id: $CHAT_ID"
  echo
  echo "Gmail: create an App Password at https://myaccount.google.com/apppasswords"
  echo "(needs 2-Step Verification turned on). Leave the address blank to skip email."
  ask "Gmail address to send from: " GM_USER
  GM_PASS=""; GM_TO=""
  if [ -n "$GM_USER" ]; then
    asks "16-letter App Password (hidden): " GM_PASS
    GM_PASS=${GM_PASS// /}
    ask "Send alerts to [$GM_USER]: " GM_TO
    GM_TO=${GM_TO:-$GM_USER}
  fi
  umask 077
  cat > "$ENVF" <<EOF
TELEGRAM_BOT_TOKEN="$TG_TOKEN"
TELEGRAM_CHAT_ID="$CHAT_ID"
SMTP_USER="$GM_USER"
SMTP_PASS="$GM_PASS"
EMAIL_TO="$GM_TO"
EOF
  chmod 600 "$ENVF"
fi

echo "== 4/4  Starting the always-on service =="
cat > /etc/systemd/system/$SVC.service <<EOF
[Unit]
Description=Showtime watcher (Avengers Doomsday @ Phoenix Market City)
After=network-online.target
Wants=network-online.target

[Service]
User=showtime
WorkingDirectory=$DIR
EnvironmentFile=$ENVF
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 $DIR/watch.py --formats english
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
echo "Done. The watcher checks every ~3 minutes and survives reboots."
echo "  Live log : sudo journalctl -u $SVC -f"
echo "  Status   : systemctl status $SVC"
echo "  Stop     : sudo systemctl disable --now $SVC"
