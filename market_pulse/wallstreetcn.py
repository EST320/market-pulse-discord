"""Wallstreetcn live-news tracker: pushes new flash news for one channel to Discord.

Usage:
    python -m market_pulse.wallstreetcn us    # or: a, hk
"""

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

from market_pulse.discord import post_webhook
from market_pulse.paths import STATE_DIR

# Wallstreetcn's web frontend moved to the awtmt.com API domain. The old
# api-prod.wallstreetcn.com has dropped packets from datacenter IPs (including
# GitHub Actions) since 2026-09-17. Paths, parameters and response structure
# are identical on the new domain.
API_URL = "https://api-one-wscn.awtmt.com/apiv1/content/lives"

PAGE_SIZE = 100
MAX_PAGES = 10
MAX_SEND_PER_RUN = 100
FIRST_RUN_SEND = 10
DISCORD_DELAY_SECONDS = 0.55
RETENTION_SECONDS = 12 * 3600
# Items sometimes reach the feed long after their display time: measured over
# 12 hours, most arrived within two minutes but a few took 9 to 13 minutes,
# and about 1% took longer than the 15-minute window this used to be, which
# dropped them for good. Dedup is by ID, so a wide window only costs a longer
# look-back. It must stay well inside RETENTION_SECONDS, or an old item that
# was already sent would be forgotten and sent again.
MAX_NEWS_AGE_SECONDS = 3 * 3600
API_MAX_RETRIES = 3
API_RETRY_BACKOFF_SECONDS = 3

# Shown as the title when an item has neither a title nor any content to derive
# one from. Kept in Chinese because the feed itself is Chinese.
FALLBACK_TITLE = "华尔街见闻快讯"


@dataclass(frozen=True)
class Channel:
    key: str
    api_channel: str
    live_url: str
    state_file: Path
    webhook_env: str
    color: int
    author: str


CHANNELS = {
    "us": Channel(
        key="us",
        api_channel="us-stock-channel",
        live_url="https://wallstreetcn.com/live/us-stock",
        state_file=STATE_DIR / "seen_us.json",
        webhook_env="DISCORD_WEBHOOK_URL",
        color=5793266,
        author="wallstreetcn · us-stock",
    ),
    "a": Channel(
        key="a",
        api_channel="a-stock-channel",
        live_url="https://wallstreetcn.com/live/a-stock",
        state_file=STATE_DIR / "seen_a.json",
        webhook_env="DISCORD_WEBHOOK_URL_A",
        color=15548997,
        author="wallstreetcn · a-stock",
    ),
    "hk": Channel(
        key="hk",
        api_channel="hk-stock-channel",
        live_url="https://wallstreetcn.com/live/hk-stock",
        state_file=STATE_DIR / "seen_hk.json",
        webhook_env="DISCORD_WEBHOOK_URL_HK",
        color=3066993,
        author="wallstreetcn · hk-stock",
    ),
}

# One Session reuses TCP/TLS connections: a run makes API requests plus N
# Discord posts, and reusing the connection is faster than a fresh handshake
# each time and less likely to get rate limited.
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0"})


def api_headers(channel):
    return {
        "Accept": "application/json, text/plain, */*",
        "Referer": channel.live_url,
        "Origin": "https://wallstreetcn.com",
    }


def load_seen(state_file):
    if not state_file.exists():
        return {}
    try:
        return json.loads(state_file.read_text(encoding="utf-8")).get("seen", {})
    except (json.JSONDecodeError, OSError) as exc:
        # Don't crash on a corrupt state file; continue with empty state, which
        # makes this run take the first-run path.
        print(f"Failed to read state file, treating it as empty: {exc!r}")
        return {}


def save_seen(state_file, seen):
    cutoff = time.time() - RETENTION_SECONDS
    pruned = {k: v for k, v in seen.items() if v > cutoff}
    state_file.write_text(
        json.dumps({"seen": pruned}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def fetch_page(channel, cursor=0):
    params = {
        "channel": channel.api_channel,
        "client": "pc",
        "cursor": cursor,
        "limit": PAGE_SIZE,
    }
    last_exc = None
    for attempt in range(1, API_MAX_RETRIES + 1):
        try:
            response = SESSION.get(
                API_URL, params=params, headers=api_headers(channel), timeout=20
            )
            response.raise_for_status()
            payload = response.json().get("data", {})
            items = payload.get("items", [])
            if not isinstance(items, list):
                raise RuntimeError(f"Unexpected API items type: {type(items)}")
            return items, payload.get("next_cursor", 0)
        except (requests.exceptions.RequestException, ValueError, RuntimeError) as exc:
            last_exc = exc
            if attempt < API_MAX_RETRIES:
                wait = API_RETRY_BACKOFF_SECONDS * attempt
                print(f"fetch_page attempt {attempt} failed ({exc!r}), retrying in {wait}s")
                time.sleep(wait)
    raise last_exc


def item_to_news(item):
    news_id = str(item.get("id", "")).strip()
    if not news_id:
        return None

    title = str(item.get("title", "")).strip()
    content = str(item.get("content_text", "")).strip()
    # Strip the trailing "(from the Wallstreetcn app)" signature.
    content = re.sub(r"\s*[（(]来自华尔街见闻APP[）)]\s*$", "", content).strip()

    if not title:
        # Untitled items usually lead with a 【bracketed headline】.
        bracket_match = re.match(r"^【([^】]+)】\s*", content)
        if bracket_match:
            title = bracket_match.group(1)
            content = content[bracket_match.end():].strip()
        else:
            lines = content.split("\n", 1)
            title = lines[0].strip() or FALLBACK_TITLE
            content = lines[1].strip() if len(lines) > 1 else ""

    title = re.sub(r"\s{2,}", " ", title.replace("\n", " ").replace("\r", " ")).strip()[:250]
    content = content[:3900]

    display_time = item.get("display_time")
    display_ts = display_time if isinstance(display_time, (int, float)) else None
    timestamp = (
        datetime.fromtimestamp(display_ts, tz=timezone.utc).isoformat()
        if display_ts is not None
        else None
    )

    return {
        "id": news_id,
        "title": title,
        "content": content,
        "timestamp": timestamp,
        "display_ts": display_ts,
    }


def collect_new_items(channel, seen_ids):
    """Return unseen items inside the age window, oldest first."""
    collected = {}
    cursor = 0
    now = time.time()

    for _ in range(MAX_PAGES):
        raw_items, next_cursor = fetch_page(channel, cursor)
        if not raw_items:
            break

        page_news = [n for n in (item_to_news(x) for x in raw_items) if n]
        for news in page_news:
            if news["display_ts"] is None:
                continue
            if now - news["display_ts"] > MAX_NEWS_AGE_SECONDS:
                continue
            if news["id"] not in seen_ids:
                collected[news["id"]] = news

        # No early stop at the first seen item: an item that surfaces late sits
        # at its (older) display-time position, possibly behind seen items, so
        # the whole window has to be read.
        # The oldest item on this page is already outside the age window; later
        # pages are only older, so there is no point requesting them.
        oldest_ts = min(
            (n["display_ts"] for n in page_news if n["display_ts"] is not None),
            default=None,
        )
        if oldest_ts is not None and now - oldest_ts > MAX_NEWS_AGE_SECONDS:
            break

        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor

    return list(reversed(collected.values()))


def build_embed(channel, news):
    safe_title = re.sub(r"\s{2,}", " ", news["title"].replace("\n", " ").replace("\r", " ")).strip()
    text = f"[{safe_title}]({channel.live_url})"

    if news["content"]:
        body = re.sub(r"\n{2,}", "\n", news["content"].replace("\r\n", "\n").replace("\r", "\n"))
        text += "\n" + body.replace("\n", "\n\n")

    embed = {
        "color": channel.color,
        "author": {"name": channel.author, "url": channel.live_url},
        "description": text[:4096],
    }

    if news["timestamp"]:
        embed["timestamp"] = news["timestamp"]

    return embed


def post_to_discord(channel, webhook_url, news):
    post_webhook(
        webhook_url,
        {"embeds": [build_embed(channel, news)], "allowed_mentions": {"parse": []}},
        session=SESSION,
    )


def run(channel):
    webhook_url = os.environ[channel.webhook_env]

    seen = load_seen(channel.state_file)
    seen_ids = set(seen)
    first_run = not seen_ids

    try:
        new_items = collect_new_items(channel, seen_ids)
    except (requests.exceptions.RequestException, ValueError, RuntimeError) as exc:
        # Skip this round on fetch failure; the next trigger two minutes later
        # picks it up, and the job doesn't go red.
        print(f"[{channel.key}] Fetch failed, skipping this run: {exc!r}")
        return

    to_send = new_items[-FIRST_RUN_SEND:] if first_run else new_items[:MAX_SEND_PER_RUN]

    sent = 0
    try:
        for news in to_send:
            post_to_discord(channel, webhook_url, news)
            # Record each item as it is sent: if a later one raises, the ones
            # already delivered are not pushed again next round.
            seen[news["id"]] = time.time()
            sent += 1
            time.sleep(DISCORD_DELAY_SECONDS)
    finally:
        if first_run:
            # A first run only sends the latest few; mark the rest as seen so
            # the next round doesn't treat them as new.
            now = time.time()
            for news in new_items:
                seen.setdefault(news["id"], now)
        # Persist even if the loop failed midway, so dedup state is never lost.
        save_seen(channel.state_file, seen)
        print(f"[{channel.key}] Found {len(new_items)} new item(s), sent {sent}.")


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in CHANNELS:
        sys.exit(f"usage: python -m market_pulse.wallstreetcn {{{'|'.join(CHANNELS)}}}")
    run(CHANNELS[args[0]])


if __name__ == "__main__":
    main()
