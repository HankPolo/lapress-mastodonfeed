#!/usr/bin/env python3
"""Post new items from an RSS feed to Mastodon. Runs from GitHub Actions."""

import json
import os
import re
import sys
import time
from pathlib import Path

import feedparser
import requests

FEED_URL  = os.environ.get("FEED_URL", "https://lapublicpress.org/feed/")
INSTANCE  = os.environ.get("MASTODON_INSTANCE", "").strip().rstrip("/")
TOKEN     = os.environ.get("MASTODON_TOKEN", "").strip()
MAX_CHARS = int(os.environ.get("MAX_CHARS", "480"))
DRY_RUN   = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
SEED_POST = os.environ.get("SEED_POST", "").lower() in ("1", "true", "yes")

STATE_FILE = Path("seen.json")
KEEP_KEYS  = 800  # bound the memory file so it doesn't grow forever

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def load_state():
    """Returns (list_of_keys, seeded). seeded=False on the very first run."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())["seen"], True
        except (json.JSONDecodeError, KeyError):
            print("seen.json unreadable; treating as first run")
    return [], False


def save_state(keys):
    keys = list(dict.fromkeys(keys))[-KEEP_KEYS:]  # dedupe, keep recent
    STATE_FILE.write_text(json.dumps({"seen": keys}, indent=2) + "\n")


def fetch_entries():
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        r = requests.get(FEED_URL, headers=headers, timeout=45)
        r.raise_for_status()
    except requests.RequestException as e:
        die(f"feed fetch failed: {e}")

    parsed = feedparser.parse(r.content)
    if parsed.bozo and not parsed.entries:
        die(f"feed parse failed: {parsed.get('bozo_exception')}")
    print(f"feed HTTP {r.status_code}, {len(parsed.entries)} entries")
    return parsed.entries


def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&#8217;", "'"), ("&#8216;", "'"), ("&#8220;", '"'),
                 ("&#8221;", '"'), ("&#8212;", "—"), ("&#8230;", "…")):
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", text).strip()


def build_status(entry):
    title = clean(entry.get("title", "Untitled"))
    link  = entry.get("link", "").strip()
    room  = MAX_CHARS - len(link) - 4
    if len(title) > room:
        title = title[: max(room - 1, 10)].rstrip() + "…"
    return f"{title}\n\n{link}"


def post_to_mastodon(text):
    url = f"{INSTANCE}/api/v1/statuses"
    for attempt in range(4):
        try:
            r = requests.post(
                url,
                headers={"Authorization": f"Bearer {TOKEN}"},
                data={"status": text, "visibility": "public"},
                timeout=30,
            )
        except requests.RequestException as e:
            print(f"  attempt {attempt + 1} failed: {e}")
            time.sleep(5 * 2 ** attempt)
            continue

        if r.status_code in (200, 201):
            return r.json().get("url", "(posted)")
        if r.status_code == 429:
            wait = min(int(r.headers.get("Retry-After", 60)), 300)
            print(f"  rate limited; waiting {wait}s")
            time.sleep(wait)
            continue
        print(f"  HTTP {r.status_code}: {r.text[:300]}")
        return None
    return None


def main():
    if not INSTANCE or not TOKEN:
        die("MASTODON_INSTANCE and MASTODON_TOKEN must be set")

    seen_list, seeded = load_state()
    seen = set(seen_list)
    entries = fetch_entries()

    def key(e):
        return e.get("id") or e.get("link")

    # feed is newest-first; post oldest-first so the timeline reads correctly
    new = [e for e in reversed(entries) if key(e) not in seen]

    if not seeded:
        print("FIRST RUN — recording existing items without posting")
        if SEED_POST and entries:
            text = build_status(entries[0])
            print(f"SEED_POST -> {text!r}")
            if not DRY_RUN:
                print(f"  {post_to_mastodon(text)}")
        save_state([key(e) for e in entries])
        return

    if not new:
        print("no new items")
        return

    for entry in new:
        text = build_status(entry)
        print(f"posting: {text!r}")
        if DRY_RUN:
            seen_list.append(key(entry))
            continue
        result = post_to_mastodon(text)
        if result:
            print(f"  ok -> {result}")
            seen_list.append(key(entry))
            save_state(seen_list)      # save after each success
        else:
            print("  failed; will retry next run")
        time.sleep(3)


if __name__ == "__main__":
    main()
