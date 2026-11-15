#!/usr/bin/env python3
"""
ucla_seat_watch.py - watch UCLA's public Schedule of Classes and push an alert
to your iPhone when a class you want gets an open seat (or, for classes you
mark waitlist_ok, an open waitlist spot).

What it does
  * Reads the PUBLIC Schedule of Classes page (no login, no cookies).
  * Prints the status of each class on every check.
  * When a spot opens it sends a push notification to your phone (via the free
    ntfy app), a desktop notification, and a terminal bell. Tapping the phone
    notification opens MyUCLA so YOU can log in and enroll.

What it deliberately does not do
  * It never asks for, stores, or sends your UCLA logon, and it never touches
    MyUCLA enrollment. Enrolling by software is against UCLA Registrar policy,
    so that step stays manual.

iPhone setup (one time, about a minute)
  1. Install "ntfy" from the App Store and allow notifications.
  2. Run:  python3 ucla_seat_watch.py --setup-phone
     It creates a private topic name, shows it, and sends a test push once
     you have subscribed to it in the app.

Everyday use
  python3 ucla_seat_watch.py                 # check every 5 minutes
  python3 ucla_seat_watch.py --interval 180  # check every 3 minutes
  python3 ucla_seat_watch.py --once          # single check, then exit
  python3 ucla_seat_watch.py --test-push     # send a test push and exit
  python3 ucla_seat_watch.py --open          # also open MyUCLA on this computer

The computer running this must stay awake and online; the script is what
does the checking. To run it without your computer, use the GitHub Actions
workflow that comes with it (seat-watch.yml), which runs
    python3 ucla_seat_watch.py --once --state state.json
every 5 minutes; --state remembers the last status between runs.
Python 3.8+, standard library only.
"""

import argparse
import html
import json
import os
import platform
import random
import re
import secrets
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
TERM = "27W"  # Winter 2027 (YY + F/W/S, e.g. 27S = Spring 2027)

# catlg is UCLA's internal catalog code: number zero-padded to 4 digits, then
# the suffix letter; a multiple-listed "M" prefix moves to the end.
#   201A  -> "0201A"       M183B -> "0183B M"       31 -> "0031"
#
# waitlist_ok: True  -> an open WAITLIST spot is worth an urgent alert too
#              False -> urgent alert only for an open SEAT
COURSES = [
    {
        "label": "EC ENGR 201A",
        "subject_name": "Electrical and Computer Engineering (EC ENGR)",
        "subj": "EC ENGR",
        "catlg": "0201A",
        "waitlist_ok": False,
    },
    {
        "label": "MECH&AE M183B",
        "subject_name": "Mechanical and Aerospace Engineering (MECH&AE)",
        "subj": "MECH&AE",
        "catlg": "0183B M",
        "waitlist_ok": True,
    },
]

SOC_RESULTS = "https://sa.ucla.edu/ro/public/soc/Results"
MYUCLA = "https://my.ucla.edu"
NTFY_SERVER = "https://ntfy.sh"
TOPIC_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ntfy_topic.txt")
MIN_INTERVAL = 120     # seconds; be a polite visitor to the public page
REMIND_EVERY = 900     # while a spot stays open, repeat the alert this often (seconds)
KINDS = ("Open", "Waitlist", "Closed by Dept", "Closed", "Cancelled", "Tentative")

# --------------------------------------------------------------------------
# Fetch + parse the public schedule
# --------------------------------------------------------------------------
# Status / waitlist wording seen on the live schedule:
#   "Open | 67 of 150 Enrolled | 83 Spots Left"     / "No Waitlist" or "1 of 30 Taken"
#   "Waitlist | Class Full (395)"                   / "16 of 20 Taken"
#   "Closed | Class Full (50)"                      / "No Waitlist" or "Waitlist Full (10)"
#   "Closed by Dept ... (0 capacity, 0 enrolled, 44 waitlisted)" / "44 Waitlisted, Contact ..."
ROW_RE = re.compile(
    r'class="statusColumn" id="(?P<row>\d{9}_[^"]+)-status_data">(?P<status>.*?)</div>\s*'
    r'<div class="waitlistColumn"[^>]*>(?P<wait>.*?)</div>',
    re.S,
)
TAKEN_RE = re.compile(r"(\d+)\s+of\s+(\d+)\s+Taken", re.I)


def results_url(course):
    q = {
        "SubjectAreaName": course["subject_name"],
        "t": TERM,
        "sBy": "subject",
        "subj": course["subj"],
        "catlg": course["catlg"],
        "cls_no": "%",
        "btnIsInIndex": "btn_inIndex",
    }
    return SOC_RESULTS + "?" + urllib.parse.urlencode(q)


def fetch(url, timeout=30):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (personal seat-watch script; one request per class every few minutes)",
            "Accept": "text/html",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _clean(fragment):
    text = re.sub(r"<br\s*/?>", " | ", fragment)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip(" |")


def _kind(status):
    for k in KINDS:
        if status.startswith(k):
            return k
    return "Unknown"


def parse_sections(page):
    """Return a list of dicts, one per lecture/primary section on the page."""
    sections = []
    for m in ROW_RE.finditer(page):
        row = m.group("row")
        class_id = row.split("_", 1)[0]
        name = re.search(r'title="Class Detail for %s">([^<]+)<' % class_id, page)
        status = _clean(m.group("status"))
        sections.append(
            {
                "class_id": class_id,
                "section": name.group(1).strip() if name else class_id,
                "status": status,
                "waitlist": _clean(m.group("wait")),
                "kind": _kind(status),
            }
        )
    return sections


def opportunity(section, waitlist_ok):
    """Return 'SEAT OPEN', 'WAITLIST SPOT OPEN', or None."""
    if section["kind"] == "Open":
        return "SEAT OPEN"
    if waitlist_ok and section["kind"] == "Waitlist":
        m = TAKEN_RE.search(section["waitlist"])
        if m:
            return "WAITLIST SPOT OPEN" if int(m.group(1)) < int(m.group(2)) else None
        if "full" not in section["waitlist"].lower() and "no waitlist" not in section["waitlist"].lower():
            return "WAITLIST SPOT OPEN"   # unfamiliar wording: better a false alarm than a miss
    return None


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------
def load_topic():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic and os.path.exists(TOPIC_FILE):
        with open(TOPIC_FILE, encoding="utf-8") as fh:
            topic = fh.read().strip()
    return topic


def push_phone(topic, title, message, urgent):
    """Send a push through ntfy. Returns True on success."""
    if not topic:
        return False
    req = urllib.request.Request(
        NTFY_SERVER + "/" + urllib.parse.quote(topic, safe=""),
        data=message.encode("utf-8"),
        headers={
            "Title": title.encode("ascii", "replace").decode(),
            "Priority": "urgent" if urgent else "default",
            "Tags": "rotating_light" if urgent else "information_source",
            "Click": MYUCLA,
        },
        method="POST",
    )
    last = None
    for attempt in range(3):
        try:
            urllib.request.urlopen(req, timeout=15).close()
            return True
        except Exception as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    print("   !! phone push FAILED: %s" % last)
    return False


def notify_desktop(title, message):
    print("\a", end="", flush=True)  # terminal bell
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(
                ["osascript",
                 "-e", "on run argv",
                 "-e", 'display notification (item 1 of argv) with title (item 2 of argv) sound name "Glass"',
                 "-e", "end run", message, title],
                check=False, timeout=10)
        elif system == "Linux":
            subprocess.run(["notify-send", "-u", "critical", title, message], check=False, timeout=10)
        elif system == "Windows":
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        pass  # the printed line and bell are still there


def alert(topic, title, message, urgent):
    print("   >>> %s" % title)
    notify_desktop(title, message)
    if topic and push_phone(topic, title, message, urgent):
        print("   >>> pushed to phone")


def setup_phone():
    topic = load_topic()
    if topic:
        print("Using your existing topic.")
    else:
        topic = "ucla-seats-" + secrets.token_urlsafe(12)
        with open(TOPIC_FILE, "w", encoding="utf-8") as fh:
            fh.write(topic + "\n")
        try:
            os.chmod(TOPIC_FILE, 0o600)
        except OSError:
            pass
        print("Created a private topic and saved it in %s" % TOPIC_FILE)
    print("\nOn your iPhone:")
    print("  1. Install 'ntfy' from the App Store, open it, and allow notifications.")
    print("  2. Tap +  (Subscribe to topic) and enter exactly:\n\n        %s\n" % topic)
    print("  3. Optional but recommended: Settings > Notifications > ntfy > turn on")
    print("     Time Sensitive Notifications, so alerts get through a Focus mode.")
    print("\nKeep the topic name private - anyone who knows it can read these alerts.")
    try:
        input("\nPress Enter once you have subscribed, and a test push will be sent... ")
    except EOFError:
        pass
    return test_push(topic)


def test_push(topic=None):
    topic = topic or load_topic()
    if not topic:
        print("No phone topic set up yet. Run:  python3 ucla_seat_watch.py --setup-phone")
        return 1
    ok = push_phone(topic, "UCLA seat watch: test",
                    "Phone alerts are working. Tap to open MyUCLA.", urgent=True)
    print("Test push sent - check your phone." if ok else "Test push failed (see error above).")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# Remembering the last status between separate runs (--state)
# --------------------------------------------------------------------------
def load_state(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        return {tuple(k.split("|", 1)): v for k, v in raw.items()}
    except (OSError, ValueError):
        return {}   # unreadable state: start fresh rather than crash


def save_state(path, previous):
    if not path:
        return
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"|".join(k): v for k, v in previous.items()}, fh, indent=1)


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
def check_all(previous, topic, open_browser):
    """Check every course once. Returns how many courses could not be checked."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    now = time.time()
    failures = 0
    for course in COURSES:
        try:
            sections = parse_sections(fetch(results_url(course)))
        except Exception as exc:
            print("[%s] %s: could not check (%s)" % (stamp, course["label"], exc))
            failures += 1
            continue

        if not sections:
            print("[%s] %s: no sections found - check TERM/subj/catlg, or the page layout changed"
                  % (stamp, course["label"]))
            failures += 1
            continue

        for s in sections:
            key = (course["label"], s["class_id"])
            name = "%s %s" % (course["label"], s["section"])
            line = "%s: %s  [waitlist: %s]" % (name, s["status"], s["waitlist"])
            old = previous.get(key)
            s["opp"] = opportunity(s, course.get("waitlist_ok", False))
            s["alerted_at"] = old["alerted_at"] if old else 0.0
            previous[key] = s

            if old is None:
                print("[%s] %s" % (stamp, line))
            elif (old["status"], old["waitlist"]) != (s["status"], s["waitlist"]):
                print("[%s] CHANGED  %s   (was: %s  [waitlist: %s])"
                      % (stamp, line, old["status"], old["waitlist"]))
            else:
                print("[%s] no change  %s" % (stamp, name))

            if s["opp"]:
                is_new = old is None or old["opp"] != s["opp"]
                if is_new or now - s["alerted_at"] >= REMIND_EVERY:
                    title = "%s: %s" % (s["opp"], name)
                    body = "%s | waitlist: %s. Log in to MyUCLA and enroll now." % (s["status"], s["waitlist"])
                    alert(topic, title if is_new else "STILL OPEN - " + title, body, urgent=True)
                    s["alerted_at"] = now
                    if open_browser and is_new:
                        webbrowser.open(MYUCLA)
            elif old is not None and old["kind"] != s["kind"]:
                # Not a spot for you yet, but the class changed state - worth a quiet heads-up.
                alert(topic, "Status changed: %s" % name,
                      "%s | waitlist: %s" % (s["status"], s["waitlist"]), urgent=False)
    return failures


def main():
    ap = argparse.ArgumentParser(description="Watch UCLA Schedule of Classes and alert your phone when a spot opens.")
    ap.add_argument("--interval", type=int, default=300,
                    help="seconds between checks (default 300, minimum %d)" % MIN_INTERVAL)
    ap.add_argument("--once", action="store_true", help="check once and exit")
    ap.add_argument("--open", action="store_true", help="also open MyUCLA in this computer's browser on an alert")
    ap.add_argument("--state", metavar="FILE", help="remember the last status in FILE between runs (for scheduled --once runs)")
    ap.add_argument("--setup-phone", action="store_true", help="create a private ntfy topic and send a test push")
    ap.add_argument("--test-push", action="store_true", help="send a test push to your phone and exit")
    args = ap.parse_args()

    if args.setup_phone:
        return setup_phone()
    if args.test_push:
        return test_push()

    interval = max(args.interval, MIN_INTERVAL)
    topic = load_topic()
    print("Watching %d class(es) for term %s. Ctrl+C to stop." % (len(COURSES), TERM))
    for c in COURSES:
        print("  - %s: alert on open seat%s" % (c["label"], " or open waitlist spot" if c.get("waitlist_ok") else ""))
    if topic:
        print("Phone alerts: ON\n")
    else:
        print("Phone alerts: OFF - run with --setup-phone first to get iPhone notifications.\n")

    previous = load_state(args.state)
    try:
        while True:
            failures = check_all(previous, topic, args.open)
            save_state(args.state, previous)
            if args.once:
                # Non-zero exit makes a scheduled run show up as failed if a check didn't work.
                return 2 if failures else 0
            time.sleep(interval + random.uniform(0, interval * 0.1))
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
