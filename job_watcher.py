#!/usr/bin/env python3
"""
job_watcher.py - ping your phone when a new SWE internship posting appears.

Sources:
  1. github.com/speedyapply/2027-SWE-College-Jobs  (parsed from the raw markdown)
  2. LinkedIn job search (optional, best-effort - see NOTE below)

Notifications: ntfy.sh (free, no account), Pushover, or a Discord webhook.

Usage:
    python3 job_watcher.py                 # check once, notify on anything new
    python3 job_watcher.py --seed          # record what's live now, notify nothing
    python3 job_watcher.py --loop 900      # check every 15 min, forever
    python3 job_watcher.py --test          # send a test notification and exit
    python3 job_watcher.py --dry-run       # print what it would send

Zero dependencies - Python 3.8+ standard library only.

NOTE on LinkedIn: LinkedIn actively blocks automated requests. The guest
endpoint used here works reasonably from a home IP but is usually blocked
from cloud/CI IPs (GitHub Actions included). It is OFF by default. The
reliable way to get LinkedIn postings on your phone fast is LinkedIn's own
job alert on your saved search, set to "Daily" with push turned on.
"""

import argparse
import html
import json
import os
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# CONFIG - edit here, or override with environment variables
# --------------------------------------------------------------------------

# Your ntfy topic. Treat it like a password: anyone who knows it can send you
# notifications. Install the "ntfy" app (iOS/Android), subscribe to this topic.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")

# Optional alternatives (leave blank to skip)
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")

# Which lists to watch from the GitHub repo.
REPO = os.environ.get("JOB_REPO", "speedyapply/2027-SWE-College-Jobs")
SOURCES = {
    "Intern USA": "README.md",
    # Uncomment any you also want watched:
    # "New Grad USA": "NEW_GRAD_USA.md",
    # "Intern Intl": "INTERN_INTL.md",
    # "New Grad Intl": "NEW_GRAD_INTL.md",
}

# Filters. Empty list = no filtering (you get every new posting).
# Matching is case-insensitive substring matching.
TITLE_MUST_MATCH_ANY = []          # e.g. ["intern", "software", "swe"]
TITLE_MUST_NOT_MATCH_ANY = []      # e.g. ["phd", "master", "hardware", "sales"]
LOCATION_MUST_MATCH_ANY = []       # e.g. ["new york", "ny", "nyc", "remote"]

# LinkedIn (optional, off by default - see NOTE at top)
LINKEDIN_ENABLED = os.environ.get("LINKEDIN_ENABLED", "0") == "1"
LINKEDIN_KEYWORDS = os.environ.get("LINKEDIN_KEYWORDS", "software engineering intern")
LINKEDIN_LOCATION = os.environ.get("LINKEDIN_LOCATION", "New York, New York, United States")
LINKEDIN_POSTED_WITHIN_SECONDS = int(os.environ.get("LINKEDIN_TPR", "86400"))

# Where to remember what we've already seen.
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "seen_jobs.json"))

# Send at most this many individual (tappable) notifications per check.
# Anything beyond that is rolled into one summary notification.
MAX_INDIVIDUAL_ALERTS = int(os.environ.get("MAX_ALERTS", "8"))

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

# --------------------------------------------------------------------------


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def fetch(url, data=None, headers=None, timeout=30, retries=3):
    """GET (or POST if data) a URL, return the decoded body."""
    headers = dict(headers or {})
    headers.setdefault("User-Agent", USER_AGENT)
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001 - network flakiness of every shape
            last_err = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt + random.random())
    raise last_err


# --------------------------------------------------------------------------
# Source 1: the GitHub repo's markdown tables
# --------------------------------------------------------------------------

LINK_RE = re.compile(r'href="([^"]+)"')
TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(s):
    return html.unescape(TAG_RE.sub("", s)).strip()


def parse_markdown_jobs(markdown, source_label):
    """
    Rows look like:
      | <a href=company><strong>Name</strong></a> | Position | Location | $55/hr |
        <a href=APPLY_URL><img .../></a> | 1d |
    The salary column is missing on some rows, so columns are counted from the end.
    """
    jobs = []
    section = ""
    for line in markdown.split("\n"):
        stripped = line.strip()
        if stripped.startswith("###"):
            section = strip_tags(stripped.lstrip("#")).split(":")[0].strip()
            continue
        if not stripped.startswith("| <a href"):
            continue

        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 5:
            continue

        company = strip_tags(cells[0])
        position = strip_tags(cells[1])
        location = strip_tags(cells[2])
        age = strip_tags(cells[-1])
        posting_cell = cells[-2]
        salary = strip_tags(cells[3]) if len(cells) >= 6 else ""

        m = LINK_RE.search(posting_cell)
        apply_url = html.unescape(m.group(1)) if m else ""
        if not apply_url or not position:
            continue

        jobs.append({
            "key": apply_url.split("?")[0],
            "company": company,
            "position": position,
            "location": location,
            "salary": salary,
            "age": age,
            "url": apply_url,
            "source": f"{source_label}" + (f" / {section}" if section else ""),
        })
    return jobs


def get_github_jobs():
    jobs = []
    for label, filename in SOURCES.items():
        url = f"https://raw.githubusercontent.com/{REPO}/main/{filename}"
        try:
            md = fetch(url)
        except Exception as e:  # noqa: BLE001
            log(f"  ! could not fetch {filename}: {e}")
            continue
        parsed = parse_markdown_jobs(md, label)
        log(f"  {label}: {len(parsed)} postings listed")
        jobs.extend(parsed)
    return jobs


# --------------------------------------------------------------------------
# Source 2: LinkedIn (best-effort, optional)
# --------------------------------------------------------------------------

def get_linkedin_jobs():
    if not LINKEDIN_ENABLED:
        return []
    params = urllib.parse.urlencode({
        "keywords": LINKEDIN_KEYWORDS,
        "location": LINKEDIN_LOCATION,
        "f_TPR": f"r{LINKEDIN_POSTED_WITHIN_SECONDS}",
        "start": 0,
    })
    url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?{params}"
    try:
        body = fetch(url, headers={"Accept": "text/html"}, retries=2)
    except urllib.error.HTTPError as e:
        log(f"  ! LinkedIn returned HTTP {e.code} (it blocks automated traffic; "
            f"use LinkedIn's own job alert for this source)")
        return []
    except Exception as e:  # noqa: BLE001
        log(f"  ! LinkedIn fetch failed: {e}")
        return []

    jobs = []
    for card in body.split("<li>"):
        urn = re.search(r'data-entity-urn="urn:li:jobPosting:(\d+)"', card)
        if not urn:
            continue
        title = re.search(r'base-search-card__title"[^>]*>(.*?)</h3>', card, re.S)
        company = re.search(r'hidden-nested-link"[^>]*>(.*?)</a>', card, re.S)
        loc = re.search(r'job-search-card__location"[^>]*>(.*?)</span>', card, re.S)
        jobs.append({
            "key": f"linkedin:{urn.group(1)}",
            "company": strip_tags(company.group(1)) if company else "?",
            "position": strip_tags(title.group(1)) if title else "?",
            "location": strip_tags(loc.group(1)) if loc else "",
            "salary": "",
            "age": "",
            "url": f"https://www.linkedin.com/jobs/view/{urn.group(1)}/",
            "source": "LinkedIn",
        })
    log(f"  LinkedIn: {len(jobs)} postings in the last "
        f"{LINKEDIN_POSTED_WITHIN_SECONDS // 3600}h")
    return jobs


# --------------------------------------------------------------------------
# Filtering + state
# --------------------------------------------------------------------------

def passes_filters(job):
    title = job["position"].lower()
    loc = job["location"].lower()
    if TITLE_MUST_MATCH_ANY and not any(k.lower() in title for k in TITLE_MUST_MATCH_ANY):
        return False
    if any(k.lower() in title for k in TITLE_MUST_NOT_MATCH_ANY):
        return False
    if LOCATION_MUST_MATCH_ANY and not any(k.lower() in loc for k in LOCATION_MUST_MATCH_ANY):
        return False
    return True


def load_state():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
            return set(state.get("seen", [])), state
    except (FileNotFoundError, json.JSONDecodeError):
        return set(), {}


def save_state(seen, extra=None):
    # Cap the file so it can't grow without bound.
    seen_list = list(seen)[-20000:]
    payload = {"seen": seen_list, "updated": datetime.now(timezone.utc).isoformat()}
    if extra:
        payload.update(extra)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=0)
    os.replace(tmp, STATE_FILE)


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------

def notify(title, message, click_url=None, priority="default", tags="briefcase"):
    sent = False

    if NTFY_TOPIC and not NTFY_TOPIC.startswith("CHANGE-ME"):
        headers = {
            "Title": title.encode("ascii", "ignore").decode(),
            "Priority": priority,
            "Tags": tags,
            "Markdown": "yes",
        }
        if click_url:
            headers["Click"] = click_url
            headers["Actions"] = f"view, Apply now, {click_url}, clear=true"
        try:
            fetch(f"{NTFY_SERVER}/{NTFY_TOPIC}",
                  data=message.encode("utf-8"), headers=headers, retries=2)
            sent = True
        except Exception as e:  # noqa: BLE001
            log(f"  ! ntfy failed: {e}")

    if PUSHOVER_TOKEN and PUSHOVER_USER:
        try:
            data = urllib.parse.urlencode({
                "token": PUSHOVER_TOKEN, "user": PUSHOVER_USER,
                "title": title, "message": message,
                "url": click_url or "", "url_title": "Apply now",
                "priority": 1 if priority == "high" else 0,
            }).encode()
            fetch("https://api.pushover.net/1/messages.json", data=data, retries=2)
            sent = True
        except Exception as e:  # noqa: BLE001
            log(f"  ! pushover failed: {e}")

    if DISCORD_WEBHOOK:
        try:
            body = json.dumps({"content": f"**{title}**\n{message}\n{click_url or ''}"}).encode()
            fetch(DISCORD_WEBHOOK, data=body,
                  headers={"Content-Type": "application/json"}, retries=2)
            sent = True
        except Exception as e:  # noqa: BLE001
            log(f"  ! discord failed: {e}")

    if not sent:
        log("  ! no notification channel is configured - set NTFY_TOPIC")
    return sent


def describe(job):
    bits = [job["location"]]
    if job["salary"]:
        bits.append(job["salary"])
    if job["age"]:
        bits.append(f"posted {job['age']} ago")
    return f"{job['position']}\n{' · '.join(b for b in bits if b)}"


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def check_once(seed=False, dry_run=False):
    log("Checking for new postings...")
    jobs = get_github_jobs() + get_linkedin_jobs()
    if not jobs:
        log("No postings retrieved (network issue?) - leaving state untouched.")
        return 0

    jobs = [j for j in jobs if passes_filters(j)]
    seen, _ = load_state()
    new = []
    seen_now = set()
    for job in jobs:
        if job["key"] in seen_now:
            continue
        seen_now.add(job["key"])
        if job["key"] not in seen:
            new.append(job)

    if seed or not seen:
        save_state(seen | seen_now)
        log(f"Seeded state with {len(seen_now)} current postings. "
            f"From now on you'll only hear about new ones.")
        return 0

    if not new:
        log(f"Nothing new ({len(seen_now)} postings live).")
        save_state(seen | seen_now)
        return 0

    log(f"*** {len(new)} NEW posting(s) ***")
    for job in new:
        log(f"    {job['company']} - {job['position']} ({job['location']}) {job['url']}")

    if dry_run:
        log("(dry run - no notifications sent)")
        return len(new)

    for job in new[:MAX_INDIVIDUAL_ALERTS]:
        notify(title=f"New: {job['company']}",
               message=describe(job),
               click_url=job["url"],
               priority="high",
               tags="rocket")
        time.sleep(1)  # keep ntfy from rate-limiting the burst

    if len(new) > MAX_INDIVIDUAL_ALERTS:
        rest = new[MAX_INDIVIDUAL_ALERTS:]
        lines = [f"- {j['company']}: {j['position']}" for j in rest[:25]]
        notify(title=f"+{len(rest)} more new postings",
               message="\n".join(lines),
               click_url=f"https://github.com/{REPO}#top",
               priority="default")

    save_state(seen | seen_now)
    return len(new)


def main():
    ap = argparse.ArgumentParser(description="Ping your phone on new SWE internship postings.")
    ap.add_argument("--seed", action="store_true",
                    help="record current postings without notifying")
    ap.add_argument("--loop", type=int, metavar="SECONDS",
                    help="keep running, checking every N seconds (e.g. 900)")
    ap.add_argument("--dry-run", action="store_true", help="show new postings, send nothing")
    ap.add_argument("--test", action="store_true", help="send one test notification and exit")
    args = ap.parse_args()

    if args.test:
        ok = notify(title="Job watcher is live",
                    message="If you can see this on your phone, you're all set.",
                    click_url=f"https://github.com/{REPO}#top",
                    tags="white_check_mark")
        log("Test sent." if ok else "Test failed - check NTFY_TOPIC.")
        return 0 if ok else 1

    if args.loop:
        log(f"Watching every {args.loop}s. Ctrl-C to stop.")
        while True:
            try:
                check_once(seed=args.seed, dry_run=args.dry_run)
            except KeyboardInterrupt:
                log("Stopped.")
                return 0
            except Exception as e:  # noqa: BLE001 - never let the loop die
                log(f"! check failed: {e}")
            args.seed = False
            time.sleep(args.loop)

    check_once(seed=args.seed, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
