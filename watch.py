#!/usr/bin/env python3
"""
Watch District (district.in) for shows at INOX Phoenix Market City (Velachery, Chennai):
  - Avengers: Doomsday (English)
  - Dune: Part Three (IMAX)
Alerts when any NEW show is added from the first show date on (e.g. extra opening-weekend
screens), and when seats free up on shows after a cutoff date. Movies are set in WATCHES.

Stdlib only - no pip installs.

  python3 watch.py                  # loop forever, check every ~2 min (+ Telegram bot for subscribers)
  python3 watch.py --movies dune    # only some of the movies
  python3 watch.py --once           # one check + alerts, then exit
  python3 watch.py --dry-run        # one check, print only, no alerts
  python3 watch.py --test-notify    # send a test alert to the owner and exit

Alert channels (enabled by setting env vars):
  Telegram : TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (the owner's chat; gets every alert)
             Anyone who messages the bot is subscribed to all movies; /doomsday and /dune turn
             each one on/off, /status shows the latest check, /stop unsubscribes. Loop mode only.
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

CINEMA_MATCH = "phoenix market city"
# One entry per movie.
#   fmt    : District's language/format group id (the frmtid= in the page URL)
#   screen : only count shows whose screen format contains this text ("" = any screen)
#   from   : alert on any NEW show from this date on (the first show date)
#   after  : for dates after this, also alert when sold-out shows get seats back
WATCHES = [
    {"key": "doomsday", "title": "Avengers: Doomsday", "label": "Doomsday", "what": "English",
     "base": "https://www.district.in/movies/avengers-doomsday-movie-tickets-in-chennai-MV176735",
     "fmt": "Mm5dpqTqJz", "screen": "", "from": "2026-12-17", "after": "2026-12-20"},
    {"key": "dune", "title": "Dune: Part Three", "label": "Dune IMAX", "what": "IMAX",
     "base": "https://www.district.in/movies/dune-part-three-movie-tickets-in-chennai-MV207332",
     "fmt": "6qgsw71c3s", "screen": "imax", "from": "2026-12-16", "after": "2026-12-20"},
]
WATCH_BY_KEY = {w["key"]: w for w in WATCHES}

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "seen.json")
SUBS_FILE = os.path.join(HERE, "subscribers.json")
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
HEARTBEAT_HOUR_IST = 9  # one "still watching" message per day (owner only) after this hour

LAST_STATUS = {"text": "No check has run yet - try again in a few minutes."}
STATUS = {}  # movie key -> latest status section


def now_ist():
    return dt.datetime.now(IST)


def log(msg):
    print(f"[{now_ist():%Y-%m-%d %H:%M:%S} IST] {msg}", flush=True)


def url_for(w, date):
    return f"{w['base']}?frmtid={w['fmt']}&fromdate={date}"


def pretty(date):
    return dt.date.fromisoformat(date).strftime("%a %d %b")


# ---------- scraping ----------

def fetch_sessions(w, date):
    """Return (city_show_dates, other_cinemas_on_date, phoenix_sessions_on_date)."""
    req = urllib.request.Request(url_for(w, date), headers={
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
    entry = state["movieSessions"].get(f"{w['fmt'].lower()}{date}")
    if entry is None:
        raise RuntimeError(f"no session block for {date} - layout changed")

    show_dates = entry.get("meta", {}).get("showDates") or []
    page = entry.get("pageData") or {}
    cinemas = (page.get("nearbyCinemas") or []) + (page.get("farCinemas") or [])
    found, others = [], []
    for c in cinemas:
        name = c.get("cinemaInfo", {}).get("name", "")
        sessions = [s for s in c.get("sessions", [])
                    if s.get("showDate") == date  # guard against fallback/neighbouring dates
                    and w["screen"] in (s.get("scrnFmt") or "").lower()]
        if CINEMA_MATCH not in name.lower():
            if sessions:
                others.append(name.split(",")[0])
            continue
        for s in sessions:
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
                "url": url_for(w, date),
            })
    return show_dates, others, found


def check(w):
    """Return (all Phoenix sessions from w['from'] on, {date after cutoff: [other cinemas]})."""
    first = max(w["from"], now_ist().date().isoformat())  # never ask for past dates
    show_dates, others, found = fetch_sessions(w, first)
    per_date = {first: (others, found)}
    # Only fetch later dates that District says have shows somewhere in the city.
    for d in sorted(d for d in show_dates if d > first):
        time.sleep(random.uniform(1.5, 3.5))
        _, o, f = fetch_sessions(w, d)
        per_date[d] = (o, f)
    results, elsewhere = [], {}
    for d, (o, f) in per_date.items():
        results += f
        if d > w["after"] and o:
            elsewhere[d] = o
    log(f"{w['label']}: city show dates {show_dates[0] if show_dates else '-'}.."
        f"{show_dates[-1] if show_dates else '-'}")
    return results, elsewhere


# ---------- Telegram (owner + subscribers) ----------

class Subscribers:
    """Chat id -> set of movie keys. Thread-safe, persisted to subscribers.json."""

    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        try:
            with open(path) as f:
                raw = json.load(f)
            if isinstance(raw, list):  # before Dune was added everyone signed up for Doomsday
                raw = {str(x): ["doomsday"] for x in raw}
            self.subs = {int(k): set(v) for k, v in raw.items() if v}
        except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError, AttributeError):
            self.subs = {}

    def _save(self):
        with open(self.path, "w") as f:
            json.dump({str(k): sorted(v) for k, v in self.subs.items()}, f)

    def get(self, cid):
        with self.lock:
            return set(self.subs.get(cid, ()))

    def set(self, cid, movies):
        with self.lock:
            if movies:
                self.subs[cid] = set(movies)
            else:
                self.subs.pop(cid, None)
            self._save()

    def remove(self, cid):
        self.set(cid, ())

    def for_movie(self, key):
        with self.lock:
            return {cid for cid, movies in self.subs.items() if key in movies}


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


def send_telegram(text, movie=None):
    """Send to the owner, plus (if movie is given) everyone subscribed to that movie."""
    owner = owner_id()
    if not (os.environ.get("TELEGRAM_BOT_TOKEN") and owner):
        return False
    tg_send(owner, text)
    if movie:
        for cid in SUBS.for_movie(movie) - {owner}:
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


def subs_text(watches, mine):
    lines = []
    for w in watches:
        on = w["key"] in mine
        lines.append(f"{'✅' if on else '⬜'} {w['title']} ({w['what']}) - "
                     f"send /{w['key']} to turn {'off' if on else 'on'}")
    return "\n".join(lines)


def welcome_text(watches, mine, new):
    afters = {w["after"] for w in watches}
    when = f"after {pretty(afters.pop())}" if len(afters) == 1 else "after the dates already on sale"
    return (("✅ You're subscribed!\n\n" if new else "🔔 Your alerts:\n\n")
            + "Shows at INOX Phoenix Market City, Velachery (Chennai), checked every ~2 minutes:\n"
            + subs_text(watches, mine) + "\n\n"
            "You'll get a message when new shows are added (extra opening-weekend shows, or the "
            f"first shows {when}) or when seats free up on shows {when}.\n\n"
            "/status - latest check\n/stop - stop all alerts")


def telegram_listener(watches):
    """Long-poll the bot for /start, /status, /stop and per-movie toggles from anyone."""
    started, offset = time.time(), None
    keys = {w["key"]: w for w in watches}
    try:
        cmds = ([{"command": "status", "description": "Latest check"}]
                + [{"command": w["key"], "description": f"{w['title']} ({w['what']}) alerts on/off"}
                   for w in watches]
                + [{"command": "stop", "description": "Stop all alerts"}])
        tg_api("setMyCommands", {"commands": json.dumps(cmds)})
    except Exception as e:
        log(f"could not set the bot's command menu: {e!r}")
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
                word = cmd[1:].split()[0] if cmd.startswith("/") and len(cmd) > 1 else ""
                who = chat.get("first_name") or chat.get("username") or cid
                mine = SUBS.get(cid)
                if word == "stop":
                    SUBS.remove(cid)
                    tg_send(cid, "🛑 Unsubscribed from all alerts. Send /start any time to subscribe again.")
                    log(f"unsubscribed: {who} ({cid})")
                elif word == "status":
                    tg_send(cid, LAST_STATUS["text"])
                elif word in keys:
                    w = keys[word]
                    on = word not in mine
                    mine = (mine | {word}) if on else (mine - {word})
                    SUBS.set(cid, mine)
                    log(f"{who} ({cid}) turned {w['label']} {'on' if on else 'off'}")
                    tg_send(cid, f"{'✅' if on else '🔕'} {w['title']} ({w['what']}) alerts are now "
                                 f"{'ON' if on else 'OFF'}.\n\n" + subs_text(watches, mine)
                            + ("" if mine else "\n\nYou're not getting any alerts now. "
                                               "Send /start to turn everything back on."))
                else:
                    new = not mine
                    if new:
                        mine = set(keys)
                        SUBS.set(cid, mine)
                        log(f"new subscriber: {who} ({cid})")
                    tg_send(cid, welcome_text(watches, mine, new))
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


def notify(title, body, url=None, open_browser=False, loud=True, movie=None):
    """Send on every configured channel. movie='dune' etc. also sends to that movie's
    Telegram subscribers. Returns number of channels that worked."""
    log(f"ALERT{f' ({movie} subscribers)' if movie else ''}: {title} | {body}")
    full = f"{title}\n\n{body}" + (f"\n\nBook: {url}" if url else "")
    sent = 0
    for name, fn in (("telegram", lambda: send_telegram(full, movie)),
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
    if "seen" in st:  # layout from before Dune was added: everything belonged to Doomsday
        old = st.get("meta", {})
        st = {"meta": {k: old[k] for k in ("fail_streak", "last_heartbeat") if k in old},
              "watches": {"doomsday": {"seen": st["seen"], "baseline_from": old.get("baseline_from")}}}
    st.setdefault("meta", {})
    st.setdefault("watches", {})
    for w in WATCHES:
        st["watches"].setdefault(w["key"], {}).setdefault("seen", {})
    return st


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=1)


def by_date(sessions):
    out = {}
    for s in sessions:
        out.setdefault(s["date"], []).append(s)
    return dict(sorted(out.items()))


def status_section(w, sessions, elsewhere):
    lines = [f"🎬 {w['title']} ({w['what']}):"]
    for d, ss in by_date(sessions).items():
        free = sum(s["avail"] for s in ss)
        screens = len({s["screen"] for s in ss})
        lines.append(f"• {pretty(d)}: {len(ss)} shows on {screens} screen(s) - "
                     + (f"{free} seats free" if free else "sold out"))
    if not any(s["date"] > w["after"] for s in sessions):
        lines.append(f"• Nothing after {pretty(w['after'])} yet.")
    if elsewhere:
        lines.append(f"Other Chennai cinemas with {w['what']} shows after {pretty(w['after'])}:")
        lines += [f"• {pretty(d)}: {', '.join(sorted(set(v)))}" for d, v in sorted(elsewhere.items())]
    return "\n".join(lines)


def process(w, ws, sessions, alerts, open_browser):
    """Compare one movie's sessions with what we saw before and alert on changes."""
    seen = ws["seen"]
    # First run with this watch window: quietly record the existing opening-weekend shows
    # so only shows added from now on trigger alerts.
    baseline = ws.get("baseline_from") != w["from"]
    new, reopened = [], []
    for s in sessions:
        prev = seen.get(s["key"])
        if prev is None:
            if not (baseline and s["date"] <= w["after"]):
                new.append(s)
        elif s["date"] > w["after"] and prev.get("avail", 0) == 0 and s["avail"] > 0:
            # Seat-back alerts only after the cutoff; opening-weekend seats flicker constantly.
            reopened.append(s)
        seen[s["key"]] = {k: s[k] for k in ("date", "time", "avail", "status")}
    if baseline and alerts:
        ws["baseline_from"] = w["from"]
        log(f"{w['label']}: baseline recorded - {sum(1 for s in sessions if s['date'] <= w['after'])} "
            f"existing shows up to {w['after']} (no alerts for these).")

    for d, ss in by_date(sessions).items():
        log(f"  {w['label']} @ PHOENIX {d}: {len(ss)} shows on {len({s['screen'] for s in ss})} "
            f"screen(s), {sum(s['avail'] for s in ss)} seats free")
    if not any(s["date"] > w["after"] for s in sessions):
        log(f"{w['label']}: no Phoenix Market City shows after {w['after']} yet.")

    if not (alerts and (new or reopened)):
        return
    hits = sorted(new + reopened, key=lambda x: x["iso"])
    dates = ", ".join(pretty(d) for d in sorted({s["date"] for s in hits}))
    title = (f"🚨 New {w['label']} shows @ Phoenix: {dates}" if new
             else f"🎟 {w['label']} seats free again @ Phoenix: {dates}")
    lines = [f"• {pretty(s['date'])} {s['time']} {s['screen']} - {s['avail']} seats free"
             + (" (NEW)" if s in new and reopened else "") for s in hits[:15]]
    notify(title, f"{w['title']} ({w['what']}) - INOX Phoenix Market City, Velachery:\n"
           + "\n".join(lines), hits[0]["url"], open_browser, movie=w["key"])


def run_cycle(a, watches, st, alerts=True):
    """One check of every movie. Returns True if all of them worked."""
    meta, errors = st["meta"], []
    for i, w in enumerate(watches):
        if i:
            time.sleep(random.uniform(1.5, 3.5))
        try:
            sessions, elsewhere = check(w)
        except (urllib.error.URLError, TimeoutError, RuntimeError, KeyError, ValueError) as e:
            log(f"{w['label']}: check failed: {e!r}")
            errors.append(f"{w['label']}: {e}")
            STATUS[w["key"]] = f"🎬 {w['title']} ({w['what']}): ⚠️ the last check failed - retrying."
            continue
        STATUS[w["key"]] = status_section(w, sessions, elsewhere)
        process(w, st["watches"][w["key"]], sessions, alerts, a.open)
    LAST_STATUS["text"] = "\n\n".join(
        [f"🕒 Last check: {now_ist():%d %b, %I:%M %p} IST\n📍 INOX Phoenix Market City, Velachery"]
        + [STATUS[w["key"]] for w in watches if w["key"] in STATUS])

    if errors:
        meta["fail_streak"] = meta.get("fail_streak", 0) + 1
        # Alert the owner once after 3 consecutive failures, not on every retry.
        if alerts and meta["fail_streak"] == 3:
            notify("Ticket watcher problem",
                   f"3 checks in a row failed ({'; '.join(errors)}). District may be blocking the "
                   f"server or the page changed. The watcher keeps retrying.", loud=False)
        return False
    if alerts and meta.get("fail_streak", 0) >= 3:
        notify("Ticket watcher recovered", "Checks are working again.", loud=False)
    meta["fail_streak"] = 0

    # Daily heartbeat to the owner so you know it's alive.
    today = now_ist().date().isoformat()
    if alerts and meta.get("last_heartbeat") != today and now_ist().hour >= HEARTBEAT_HOUR_IST:
        counts = ", ".join(f"{w['label']} {len(SUBS.for_movie(w['key']) - {owner_id()})}"
                           for w in watches)
        notify("Ticket watcher: still watching",
               LAST_STATUS["text"] + f"\n\n👥 Friends subscribed: {counts}", loud=False)
        meta["last_heartbeat"] = today
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--movies", default=",".join(WATCH_BY_KEY),
                    help=f"comma list of movies to watch (default: all of {','.join(WATCH_BY_KEY)})")
    ap.add_argument("--formats", help=argparse.SUPPRESS)  # old option, ignored
    ap.add_argument("--interval", type=int, default=120,
                    help="seconds from the start of one check to the next in loop mode (min 60)")
    ap.add_argument("--open", action="store_true", help="on a Mac, also open the booking page on alert")
    ap.add_argument("--once", action="store_true", help="one check with alerts, then exit")
    ap.add_argument("--dry-run", action="store_true", help="one check, print only, no alerts, no state saved")
    ap.add_argument("--test-notify", action="store_true", help="send a test alert to the owner and exit")
    a = ap.parse_args()

    keys = [k.strip().lower() for k in a.movies.split(",") if k.strip()]
    bad = [k for k in keys if k not in WATCH_BY_KEY]
    if bad or not keys:
        sys.exit(f"unknown movie(s) {bad}; choose from {list(WATCH_BY_KEY)}")
    watches = [WATCH_BY_KEY[k] for k in keys]

    if a.test_notify:
        n = notify("Ticket watcher test", "If you got this, alerts work. Watching: "
                   + ", ".join(f"{w['title']} ({w['what']})" for w in watches), open_browser=a.open)
        log(f"test sent on {n} remote channel(s)")
        sys.exit(0 if n or sys.platform == "darwin" else 1)

    if a.dry_run:
        sys.exit(0 if run_cycle(a, watches, load_state(), alerts=False) else 1)

    st = load_state()
    if a.once:
        ok = run_cycle(a, watches, st)
        save_state(st)
        sys.exit(0 if ok else 1)

    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        threading.Thread(target=telegram_listener, args=(watches,), daemon=True).start()

    interval = max(60, a.interval)
    log(f"Watching '{CINEMA_MATCH}' for: "
        + ", ".join(f"{w['title']} ({w['what']}, after {w['after']})" for w in watches)
        + f" - every ~{interval}s. Ctrl+C to stop.")
    while True:
        started = time.time()
        ok = run_cycle(a, watches, st)
        save_state(st)
        if ok:  # keep a steady start-to-start rhythm regardless of how long the check took
            wait = max(30, interval - (time.time() - started))
        else:   # back off if District is failing/blocking, so we don't make it worse
            wait = min(interval * 2 ** st["meta"]["fail_streak"], 900)
        time.sleep(wait + random.uniform(0, 15))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("stopped.")
