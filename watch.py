#!/usr/bin/env python3
"""
Watch District (district.in) for Avengers: Doomsday shows at Phoenix Market City
(INOX, Velachery, Chennai) on dates AFTER a cutoff, and alert when they open.

Stdlib only - no pip installs.

  python3 watch.py                  # loop forever, check every ~3 min
  python3 watch.py --once           # one check + alerts, then exit (used by GitHub Actions)
  python3 watch.py --dry-run        # one check, print only, no alerts
  python3 watch.py --test-notify    # send a test alert on every configured channel

Alert channels (enabled by setting env vars):
  Telegram : TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
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
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json")
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
HEARTBEAT_HOUR_IST = 9  # one "still watching" message per day after this hour


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
                "key": f"{c.get('id')}-{s.get('sid')}-{date}",
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


def check(after_date, formats):
    """Return (phoenix_sessions_after_cutoff, {date: [other cinemas]})."""
    first = (dt.date.fromisoformat(after_date) + dt.timedelta(days=1)).isoformat()
    results, elsewhere = [], {}
    for fmt_name in formats:
        fmt_id = FORMATS[fmt_name]
        show_dates, others, found = fetch_sessions(fmt_id, first)
        results += found
        if others:
            elsewhere.setdefault(first, []).extend(others)
        # Only fetch later dates that District says have shows somewhere in the city.
        for d in sorted(d for d in show_dates if d > first):
            time.sleep(random.uniform(1.5, 3.5))
            _, others, found = fetch_sessions(fmt_id, d)
            results += found
            if others:
                elsewhere.setdefault(d, []).extend(others)
        log(f"{fmt_name}: city show dates after {after_date}: "
            f"{[d for d in show_dates if d > after_date] or 'none'}")
    return results, elsewhere


# ---------- notifications ----------

def send_telegram(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=20).read()
    return True


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


def notify(title, body, url=None, open_browser=False, loud=True):
    """Send on every configured channel. Returns number of channels that worked."""
    log(f"ALERT: {title} | {body}")
    full = f"{title}\n\n{body}" + (f"\n\nBook: {url}" if url else "")
    sent = 0
    for name, fn in (("telegram", lambda: send_telegram(full)),
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


def run_cycle(a, formats, st, alerts=True):
    """One check. Returns True on success, False on failure."""
    seen, meta = st["seen"], st["meta"]
    try:
        sessions, elsewhere = check(a.after, formats)
    except (urllib.error.URLError, TimeoutError, RuntimeError, KeyError, ValueError) as e:
        log(f"check failed: {e!r}")
        meta["fail_streak"] = meta.get("fail_streak", 0) + 1
        # Alert once after 3 consecutive failures, not on every retry.
        if alerts and meta["fail_streak"] == 3:
            notify("Doomsday watcher problem",
                   f"3 checks in a row failed: {e}. District may be blocking the server or "
                   f"the page changed. The watcher keeps retrying.", loud=False)
        return False

    if alerts and meta.get("fail_streak", 0) >= 3:
        notify("Doomsday watcher recovered", "Checks are working again.", loud=False)
    meta["fail_streak"] = 0

    new, reopened = [], []
    for s in sessions:
        prev = seen.get(s["key"])
        if prev is None:
            new.append(s)
        elif prev.get("avail", 0) == 0 and s["avail"] > 0:
            reopened.append(s)
        seen[s["key"]] = {k: s[k] for k in ("date", "time", "avail", "status")}

    for s in sorted(sessions, key=lambda x: x["iso"]):
        log(f"  PHOENIX {s['date']} {s['time']} {s['lang']} {s['screen']} "
            f"- {s['avail']}/{s['total']} free ({s['status']})")
    if not sessions:
        log("No Phoenix Market City shows after the cutoff yet.")

    if not alerts:
        return True

    hits = sorted(new + reopened, key=lambda x: x["iso"])
    if hits:
        dates = sorted({s["date"] for s in hits})
        lines = [f"• {s['date']} {s['time']} {s['screen']} - {s['avail']} seats free" for s in hits[:12]]
        notify(f"🚨 Doomsday @ Phoenix OPEN: {', '.join(dates)}",
               f"{len(hits)} new show(s) at INOX Phoenix Market City:\n" + "\n".join(lines),
               hits[0]["url"], a.open)

    # Daily heartbeat so you know it's alive.
    today = now_ist().date().isoformat()
    if meta.get("last_heartbeat") != today and now_ist().hour >= HEARTBEAT_HOUR_IST:
        other = "; ".join(f"{d}: {', '.join(sorted(set(v)))}" for d, v in sorted(elsewhere.items()))
        notify("Doomsday watcher: still watching",
               "No new Phoenix shows after " + a.after + " yet." +
               (f"\nOther Chennai cinemas after the cutoff: {other}" if other else ""),
               loud=False)
        meta["last_heartbeat"] = today
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--after", default="2026-12-20", help="alert only for dates after this (YYYY-MM-DD)")
    ap.add_argument("--formats", default="english", help="comma list: english,tamil")
    ap.add_argument("--interval", type=int, default=180, help="seconds between checks in loop mode (min 60)")
    ap.add_argument("--open", action="store_true", help="on a Mac, also open the booking page on alert")
    ap.add_argument("--once", action="store_true", help="one check with alerts, then exit")
    ap.add_argument("--dry-run", action="store_true", help="one check, print only, no alerts, no state saved")
    ap.add_argument("--test-notify", action="store_true", help="send a test alert and exit")
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
