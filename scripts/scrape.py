"""
scrape.py — pulls Instagram profile + recent posts for @ummadentalcare
and writes public/data/ummadentalcare.json in the shape the dashboard expects.

Uses Apify's hosted Instagram actors so we don't have to deal with logins,
sessions, or rate-limit bans ourselves:
  - apify/instagram-post-scraper   -> recent posts for the account
  - apify/instagram-profile-scraper -> follower count / profile info

Requires:
  - APIFY_API_TOKEN   (env var / GitHub secret) — token from apify.com
  - IG_USERNAME        (env var, defaults to "ummadentalcare")
  - RESULTS_LIMIT       (env var, defaults to "50") — how many recent posts to pull

Install deps:  pip install -r scripts/requirements.txt
Run locally:   APIFY_API_TOKEN=xxx python scripts/scrape.py
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from apify_client import ApifyClient

APIFY_TOKEN = os.environ.get("APIFY_API_TOKEN")
USERNAME = os.environ.get("IG_USERNAME", "ummadentalcare")
RESULTS_LIMIT = int(os.environ.get("RESULTS_LIMIT", "50"))

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "public" / "data" / f"{USERNAME}.json"


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def run_post_scraper(client: ApifyClient) -> list[dict]:
    """Pull recent posts for the account via apify/instagram-post-scraper."""
    run_input = {
        "username": [USERNAME],
        "resultsLimit": RESULTS_LIMIT,
    }
    run = client.actor("apify/instagram-post-scraper").call(run_input=run_input)
    dataset_id = run["defaultDatasetId"]
    return list(client.dataset(dataset_id).iterate_items())


def run_profile_scraper(client: ApifyClient) -> dict:
    """Pull profile-level info (followers, bio, etc) via apify/instagram-profile-scraper."""
    run_input = {"usernames": [USERNAME]}
    run = client.actor("apify/instagram-profile-scraper").call(run_input=run_input)
    dataset_id = run["defaultDatasetId"]
    items = list(client.dataset(dataset_id).iterate_items())
    return items[0] if items else {}


def normalize_type(raw_type: str | None) -> str:
    """Map Apify's `type` field to the values the dashboard understands:
    'Sidecar' (carousel), 'Video', 'Image'."""
    if not raw_type:
        return "Image"
    t = raw_type.strip().lower()
    if t in ("sidecar", "carousel", "carousel_container"):
        return "Sidecar"
    if t in ("video", "clip", "reel"):
        return "Video"
    return "Image"


def transform_post(item: dict) -> dict | None:
    ts_raw = item.get("timestamp")
    if not ts_raw:
        return None
    # Apify returns ISO timestamps like 2026-05-20T09:15:00.000Z — keep just the date
    timestamp = ts_raw[:10]

    return {
        "timestamp": timestamp,
        "type": normalize_type(item.get("type")),
        "caption": (item.get("caption") or "").strip(),
        "likesCount": item.get("likesCount") or 0,
        "commentsCount": item.get("commentsCount") or 0,
        "videoViewCount": item.get("videoViewCount") or item.get("videoPlayCount"),
        "displayUrl": item.get("displayUrl"),
        "url": item.get("url"),
    }


def main() -> None:
    if not APIFY_TOKEN:
        fail("APIFY_API_TOKEN environment variable is not set")

    client = ApifyClient(APIFY_TOKEN)

    print(f"Fetching posts for @{USERNAME} (limit {RESULTS_LIMIT})...")
    raw_posts = run_post_scraper(client)
    print(f"  -> got {len(raw_posts)} raw items")

    print(f"Fetching profile info for @{USERNAME}...")
    profile_raw = run_profile_scraper(client)

    posts = [p for p in (transform_post(item) for item in raw_posts) if p is not None]
    # newest first, matching what the dashboard expects
    posts.sort(key=lambda p: p["timestamp"], reverse=True)

    followers_count = (
        profile_raw.get("followersCount")
        or profile_raw.get("followersCountText")
        or 0
    )
    if isinstance(followers_count, str):
        followers_count = int("".join(ch for ch in followers_count if ch.isdigit()) or 0)

    data = {
        "profile": {
            "username": USERNAME,
            "followersCount": followers_count,
        },
        "posts": posts,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"Saved {len(posts)} posts + followers={followers_count} to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
