#!/usr/bin/env python3
"""
Watch District (district.in) for Avengers: Doomsday shows at Phoenix Market City
(INOX, Velachery, Chennai). Alerts when any NEW show is added from the release date on
(e.g. extra opening-weekend screens), and when seats free up on shows after a cutoff date.

Stdlib only - no pip installs.

  python3 watch.py                  # loop forever, check every ~3 min (+ Telegram bot for subscribers)
  python3 watch.py --once           # one check + alerts, then exit
  python3 watch.py --dry-run        # one check, print only, no alerts
  python3 watch.py --test-notify    # send a test alert to the owner and exit

Alert channels (enabled by setting env vars):
  Telegram : TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (the owner's chat)
             Anyone who messages the bot (/start) is subscribed to "shows open" alerts;
             /status shows the latest check, /stop unsubscribes. Loop mode only.
  Email    : SMTP_USER, SMTP_PASS, EMAIL_TO   (optional SMTP_HOST=smtp.gmail.com, SMTP_PORT=465)
  ntfy     : NTFY_TOPIC
  macOS pop-up/sound is used automatically when running on a Mac.
"""
import argparse
import datetime as dt
import json
import os
import random
import re
import smtplib
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage

BASE = "https://www.district.in/movies/avengers-doomsday-movie-tickets-in-chennai-MV176735"
FORMATS = {"english": "Mm5dpqTqJz", "tamil": "chjP5dFQh1"}
CINEMA_MATCH = "phoenix market city"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "seen.json")
SUBS_FILE = os.path.join(HERE, "subscribers.json")
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
HEARTBEAT_HOUR_IST = 9  # one "still watching" message per day (owner only) after this hour

LAST_STATUS = {"text": "No check has run yet - try again in a few minutes."}


def now_ist():
    return dt.datetime.now(IST)


def log(msg):
    print(f"[{now_ist():%Y-%m-%d %H:%M:%S} IST] {msg}", flush=True)


def url_for(fmt_id, date):
    return f"{BASE}?frmtid={fmt_id}&fromdate={date}"


# ---------- scraping ----------

def fetch_sessions(fmt_id, date):
    """Return (city_show_dates, other_cinemas_on_date, phoenix_sessions_on_date)."""
    req = urllib.request.Request(url_for(fmt_id, date), headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-IN,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8", "replace")
    m = NEXT_DATA_RE.search(html)
    if not m:
        raise RuntimeError(f"no __NEXT_DATA__ in page ({len(html)} bytes) - blocked or layout changed")
    state = json.loads(m.group(1))["props"]["pageProps"]["data"]["serverState"]
    entry = state["movieSessions"].get(f"{fmt_id.lower()}{date}")
    if entry is None:
        raise RuntimeError(f"no session block for {date} - layout changed")

    show_dates = entry.get("meta", {}).get("showDates") or []
    page = entry.get("pageData") or {}
    cinemas = (page.get("nearbyCinemas") or []) + (page.get("farCinemas") or [])
    found, others = [], []
    for c in cinemas:
        name = c.get("cinemaInfo", {}).get("name", "")
        if CINEMA_MATCH not in name.lower():
            others.append(name.split(",")[0])
            continue
        for s in c.get("sessions", []):
            if s.get("showDate") != date:  # guard against fallback/neighbouring dates
                continue
            found.append({
                # date+time+screen is stabler than District's internal session id
                "key": f"{c.get('id')}-{s.get('showTimeLocal') or s.get('showTimeISO')}-{s.get('audi')}",
                "cinema": name,
                "date": date,
                "time": s.get("showTimeText"),
                "iso": s.get("showTimeISO", ""),
                "screen": f"{s.get('audi', '')} {s.get('scrnFmt', '')}".strip(),
                "lang": s.get("lang"),
                "avail": s.get("avail", 0),
                "total": s.get("total", 0),
                "status": s.get("seatStatus", ""),
                "url": url_for(fmt_id, date),
            })
    return show_dates, others, found


def check(watch_from, after_date, formats):
    """Return (all Phoenix sessions from watch_from on, {date after cutoff: [other cinemas]})."""
    first = max(watch_from, now_ist().date().isoformat())  # never ask for past dates
    results, elsewhere = [], {}
    for fmt_name in formats:
        fmt_id = FORMATS[fmt_name]
        show_dates, others, found = fetch_sessions(fmt_id, first)
        per_date = {first: (others, found)}
        # Only fetch later dates that District says have shows somewhere in the city.
        for d in sorted(d for d in show_dates if d > first):
            time.sleep(random.uniform(1.5, 3.5))
            _, o, f = fetch_sessions(fmt_id, d)
            per_date[d] = (o, f)
        for d, (o, f) in per_date.items():
            results += f
            if d > after_date and o:
                elsewhere.setdefault(d, []).extend(o)
        log(f"{fmt_name}: city show dates {show_dates[0] if show_dates else '-'}.."
            f"{show_dates[-1] if show_dates else '-'}")
    return results, elsewhere


def pretty(date):
    return dt.date.fromisoformat(date).strftime("%a %d %b")


# ---------- Telegram (owner + subscribers) ----------

class Subscribers:
    """Chat ids that messaged the bot. Thread-safe, persisted to subscribers.json."""

    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        try:
            with open(path) as f:
                self.ids = {int(x) for x in json.load(f)}
        except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
            self.ids = set()

    def _save(self):
        with open(self.path, "w") as f:
            json.dump(sorted(self.ids), f)

    def add(self, cid):
        with self.lock:
            if cid in self.ids:
                return False
            self.ids.add(cid)
            self._save()
            return True

    def remove(self, cid):
        with self.lock:
            if cid in self.ids:
                self.ids.discard(cid)
                self._save()

    def all(self):
        with self.lock:
            return set(self.ids)


SUBS = Subscribers(SUBS_FILE)


def tg_api(method, params, timeout=20):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", data,
                                timeout=timeout) as r:
        return json.load(r)


def tg_send(chat_id, text):
    tg_api("sendMessage", {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"})


def owner_id():
    try:
        return int(os.environ.get("TELEGRAM_CHAT_ID", ""))
    except ValueError:
        return None


def send_telegram(text, everyone=False):
    owner = owner_id()
    if not (os.environ.get("TELEGRAM_BOT_TOKEN") and owner):
        return False
    tg_send(owner, text)
    if everyone:
        for cid in SUBS.all() - {owner}:
            try:
                tg_send(cid, text)
            except urllib.error.HTTPError as e:
                if e.code in (400, 403):  # user blocked the bot / chat gone
                    SUBS.remove(cid)
                    log(f"  removed subscriber {cid} (HTTP {e.code})")
                else:
                    log(f"  subscriber {cid} send failed: {e!r}")
            except Exception as e:
                log(f"  subscriber {cid} send failed: {e!r}")
            time.sleep(0.05)
    return True


def welcome_text(after):
    return ("✅ You're subscribed!\n\n"
            "You'll get a message here when new Avengers: Doomsday (English) shows are added at "
            "INOX Phoenix Market City, Velachery (Chennai) - extra opening-weekend shows and the "
            f"first shows after {pretty(after)} - or when seats free up on shows after {pretty(after)}. "
            "Checked every ~3 minutes.\n\n"
            "/status - latest check\n/stop - unsubscribe")


def telegram_listener(after):
    """Long-poll the bot for /start, /status, /stop from anyone."""
    started, offset = time.time(), None
    log("Telegram bot listening for subscribers.")
    while True:
        try:
            params = {"timeout": 50, "allowed_updates": json.dumps(["message"])}
            if offset is not None:
                params["offset"] = offset
            for u in tg_api("getUpdates", params, timeout=65).get("result", []):
                offset = u["update_id"] + 1
                m = u.get("message") or {}
                chat = m.get("chat") or {}
                cid = chat.get("id")
                # Ignore groups and messages sent before this process started.
                if not cid or chat.get("type") != "private" or m.get("date", 0) < started - 120:
                    continue
                cmd = (m.get("text") or "").strip().lower().split("@")[0]
                who = chat.get("first_name") or chat.get("username") or cid
                if cmd.startswith("/stop"):
                    SUBS.remove(cid)
                    tg_send(cid, "🛑 Unsubscribed. Send /start any time to subscribe again.")
                    log(f"unsubscribed: {who} ({cid})")
                elif cmd.startswith("/status"):
                    tg_send(cid, LAST_STATUS["text"])
                else:
                    if SUBS.add(cid):
                        log(f"new subscriber: {who} ({cid}) - total {len(SUBS.all())}")
                    tg_send(cid, welcome_text(after))
        except Exception as e:
            log(f"telegram listener error: {e!r}")
            time.sleep(15)


# ---------- other channels ----------

def send_email(subject, body):
    user, pw, to = (os.environ.get(k) for k in ("SMTP_USER", "SMTP_PASS", "EMAIL_TO"))
    if not (user and pw and to):
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(body)
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port, timeout=30) as s:
        s.login(user, pw)
        s.send_message(msg)
    return True


def send_ntfy(title, body, url):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}", data=body.encode(), method="POST",
        headers={"Title": title, "Priority": "urgent", "Tags": "rotating_light,movie_camera",
                 **({"Click": url} if url else {})})
    urllib.request.urlopen(req, timeout=15).read()
    return True


def mac_alert(title, body, url, open_browser):
    if sys.platform != "darwin":
        return
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(["osascript", "-e",
                    f'display notification "{esc(body)}" with title "{esc(title)}" sound name "Glass"'],
                   check=False)
    subprocess.Popen(["osascript", "-e", f'display alert "{esc(title)}" message "{esc(body)}"'])
    for _ in range(3):
        subprocess.run(["afplay", "/System/Library/Sounds/Sosumi.aiff"], check=False)
    subprocess.Popen(["say", title])
    if open_browser and url:
        subprocess.Popen(["open", url])


def notify(title, body, url=None, open_browser=False, loud=True, everyone=False):
    """Send on every configured channel. everyone=True also sends to Telegram subscribers.
    Returns number of channels that worked."""
    log(f"ALERT{' (all subscribers)' if everyone else ''}: {title} | {body}")
    full = f"{title}\n\n{body}" + (f"\n\nBook: {url}" if url else "")
    sent = 0
    for name, fn in (("telegram", lambda: send_telegram(full, everyone)),
                     ("email", lambda: send_email(title, body + (f"\n\nBook: {url}" if url else ""))),
                     ("ntfy", lambda: send_ntfy(title, body, url))):
        try:
            if fn():
                sent += 1
                log(f"  sent via {name}")
        except Exception as e:  # never let one channel kill the watcher
            log(f"  {name} failed: {e!r}")
    if loud:
        mac_alert(title, body, url, open_browser)
    return sent


# ---------- state ----------

def load_state():
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    st.setdefault("seen", {})
    st.setdefault("meta", {})
    return st


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=1)


def by_date(sessions):
    out = {}
    for s in sessions:
        out.setdefault(s["date"], []).append(s)
    return dict(sorted(out.items()))


def status_text(after, sessions, elsewhere):
    lines = [f"🕒 Last check: {now_ist():%d %b, %I:%M %p} IST", "",
             "🎬 INOX Phoenix Market City (English):"]
    for d, ss in by_date(sessions).items():
        free = sum(s["avail"] for s in ss)
        screens = len({s["screen"] for s in ss})
        lines.append(f"• {pretty(d)}: {len(ss)} shows on {screens} screen(s) - "
                     + (f"{free} seats free" if free else "sold out"))
    if not any(s["date"] > after for s in sessions):
        lines.append(f"• Nothing after {pretty(after)} yet.")
    if elsewhere:
        lines += ["", f"Other Chennai cinemas with shows after {pretty(after)}:"]
        lines += [f"• {pretty(d)}: {', '.join(sorted(set(v)))}" for d, v in sorted(elsewhere.items())]
    return "\n".join(lines)


def run_cycle(a, formats, st, alerts=True):
    """One check. Returns True on success, False on failure."""
    seen, meta = st["seen"], st["meta"]
    try:
        sessions, elsewhere = check(a.watch_from, a.after, formats)
    except (urllib.error.URLError, TimeoutError, RuntimeError, KeyError, ValueError) as e:
        log(f"check failed: {e!r}")
        meta["fail_streak"] = meta.get("fail_streak", 0) + 1
        # Alert the owner once after 3 consecutive failures, not on every retry.
        if alerts and meta["fail_streak"] == 3:
            notify("Doomsday watcher problem",
                   f"3 checks in a row failed: {e}. District may be blocking the server or "
                   f"the page changed. The watcher keeps retrying.", loud=False)
        return False

    if alerts and meta.get("fail_streak", 0) >= 3:
        notify("Doomsday watcher recovered", "Checks are working again.", loud=False)
    meta["fail_streak"] = 0
    LAST_STATUS["text"] = status_text(a.after, sessions, elsewhere)

    # First run with this watch window: quietly record the existing opening-weekend shows
    # so only shows added from now on trigger alerts.
    baseline = meta.get("baseline_from") != a.watch_from
    new, reopened = [], []
    for s in sessions:
        prev = seen.get(s["key"])
        if prev is None:
            if not (baseline and s["date"] <= a.after):
                new.append(s)
        elif s["date"] > a.after and prev.get("avail", 0) == 0 and s["avail"] > 0:
            # Seat-back alerts only after the cutoff; opening-weekend seats flicker constantly.
            reopened.append(s)
        seen[s["key"]] = {k: s[k] for k in ("date", "time", "avail", "status")}
    if baseline and alerts:
        meta["baseline_from"] = a.watch_from
        log(f"Baseline recorded: {sum(1 for s in sessions if s['date'] <= a.after)} existing "
            f"shows up to {a.after} (no alerts for these).")

    for d, ss in by_date(sessions).items():
        log(f"  PHOENIX {d}: {len(ss)} shows on {len({s['screen'] for s in ss})} screen(s), "
            f"{sum(s['avail'] for s in ss)} seats free")
    if not any(s["date"] > a.after for s in sessions):
        log(f"No Phoenix Market City shows after {a.after} yet.")

    if not alerts:
        return True

    if new or reopened:
        hits = sorted(new + reopened, key=lambda x: x["iso"])
        dates = ", ".join(pretty(d) for d in sorted({s["date"] for s in hits}))
        title = (f"🚨 New Doomsday shows @ Phoenix: {dates}" if new
                 else f"🎟 Seats free again @ Phoenix: {dates}")
        lines = [f"• {pretty(s['date'])} {s['time']} {s['screen']} - {s['avail']} seats free"
                 + (" (NEW)" if s in new and reopened else "") for s in hits[:15]]
        notify(title, "INOX Phoenix Market City, Velachery:\n" + "\n".join(lines),
               hits[0]["url"], a.open, everyone=True)

    # Daily heartbeat to the owner so you know it's alive.
    today = now_ist().date().isoformat()
    if meta.get("last_heartbeat") != today and now_ist().hour >= HEARTBEAT_HOUR_IST:
        subs = len(SUBS.all() - {owner_id()})
        notify("Doomsday watcher: still watching",
               LAST_STATUS["text"] + f"\n\n👥 Friends subscribed: {subs}", loud=False)
        meta["last_heartbeat"] = today
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="watch_from", default="2026-12-17",
                    help="alert on any NEW show from this date on (YYYY-MM-DD; release date)")
    ap.add_argument("--after", default="2026-12-20",
                    help="for dates after this, also alert when sold-out shows get seats back")
    ap.add_argument("--formats", default="english", help="comma list: english,tamil")
    ap.add_argument("--interval", type=int, default=180, help="seconds between checks in loop mode (min 60)")
    ap.add_argument("--open", action="store_true", help="on a Mac, also open the booking page on alert")
    ap.add_argument("--once", action="store_true", help="one check with alerts, then exit")
    ap.add_argument("--dry-run", action="store_true", help="one check, print only, no alerts, no state saved")
    ap.add_argument("--test-notify", action="store_true", help="send a test alert to the owner and exit")
    a = ap.parse_args()

    formats = [f.strip().lower() for f in a.formats.split(",") if f.strip()]
    bad = [f for f in formats if f not in FORMATS]
    if bad:
        sys.exit(f"unknown format(s) {bad}; choose from {list(FORMATS)}")

    if a.test_notify:
        n = notify("Doomsday watcher test", "If you got this, alerts work.", BASE, a.open)
        log(f"test sent on {n} remote channel(s)")
        sys.exit(0 if n or sys.platform == "darwin" else 1)

    if a.dry_run:
        sys.exit(0 if run_cycle(a, formats, load_state(), alerts=False) else 1)

    st = load_state()
    if a.once:
        ok = run_cycle(a, formats, st)
        save_state(st)
        sys.exit(0 if ok else 1)

    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        threading.Thread(target=telegram_listener, args=(a.after,), daemon=True).start()

    interval = max(60, a.interval)
    log(f"Watching '{CINEMA_MATCH}' for dates after {a.after} ({', '.join(formats)}), "
        f"every ~{interval}s. Ctrl+C to stop.")
    while True:
        ok = run_cycle(a, formats, st)
        save_state(st)
        wait = interval if ok else min(interval * 2 ** st["meta"]["fail_streak"], 900)
        time.sleep(wait + random.uniform(0, 30))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("stopped.")
