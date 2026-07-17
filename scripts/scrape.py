"""
scrape.py — Instagram scraper for @ummadentalcare via Apify
Runs daily via GitHub Actions and writes public/data/ummadentalcare.json

Uses a SINGLE Apify actor: apify/instagram-scraper, with resultsType="details".
That mode returns one dataset item = the account's profile info (followers,
bio, etc) PLUS an embedded `latestPosts` array of its most recent posts — so
we no longer need to run a separate profile-scraper and post-scraper.

⚠️ NOTE ON FIELD NAMES: `latestPosts` is the field name documented for this
actor's "details" mode based on how Instagram's own profile-page data is
shaped. Apify actors occasionally change their output schema. If this script
logs "No embedded posts found" the first time you run it, open the run in the
Apify console → Dataset → "Preview" tab, look at the real key name holding
the post list, and swap it into RAW_POSTS_KEYS below.

Requirements:
  pip install -r scripts/requirements.txt   (just `requests`)

Env vars (set as GitHub Secret):
  APIFY_API_TOKEN — Apify API token
Env vars (optional, set in the workflow file):
  IG_USERNAME    — defaults to "ummadentalcare"
  RESULTS_LIMIT  — defaults to 50 (how many posts to ask the actor for)
"""

import os
import json
import time
import logging
import requests
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────
APIFY_TOKEN    = os.environ["APIFY_API_TOKEN"]
INSTAGRAM_USER = os.environ.get("IG_USERNAME", "ummadentalcare")
RESULTS_LIMIT  = int(os.environ.get("RESULTS_LIMIT", "50"))
OUTPUT_PATH    = Path(__file__).parent.parent / "public" / "data" / f"{INSTAGRAM_USER}.json"
APIFY_BASE     = "https://api.apify.com/v2"
POLL_INTERVAL  = 5      # seconds between run-status polls
MAX_WAIT       = 300    # max seconds to wait for the run

# Possible key names Apify might use to embed the post list inside the
# profile "details" object — checked in order.
RAW_POSTS_KEYS = ["latestPosts", "posts", "latestIgtvVideos"]


# ── Helpers ───────────────────────────────────────────────────────────────
def apify_headers():
    return {"Authorization": f"Bearer {APIFY_TOKEN}", "Content-Type": "application/json"}


def start_scraper() -> str:
    url  = f"{APIFY_BASE}/acts/apify~instagram-scraper/runs"
    body = {
        "directUrls":    [f"https://www.instagram.com/{INSTAGRAM_USER}/"],
        "resultsType":   "details",
        "resultsLimit":  RESULTS_LIMIT,
        "addParentData": False,
    }
    r = requests.post(url, json=body, headers=apify_headers(), timeout=30)
    r.raise_for_status()
    run_id = r.json()["data"]["id"]
    log.info(f"instagram-scraper started  run_id={run_id}")
    return run_id


def wait_for_run(run_id: str) -> str:
    deadline = time.time() + MAX_WAIT
    while time.time() < deadline:
        r = requests.get(
            f"{APIFY_BASE}/actor-runs/{run_id}",
            headers=apify_headers(), timeout=15
        )
        r.raise_for_status()
        data   = r.json()["data"]
        status = data["status"]
        log.info(f"  run {run_id[:8]}…  status={status}")
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            if status != "SUCCEEDED":
                raise RuntimeError(f"run {run_id} ended with status {status}")
            return data["defaultDatasetId"]
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"run {run_id} did not finish within {MAX_WAIT}s")


def fetch_dataset(dataset_id: str) -> list:
    r = requests.get(
        f"{APIFY_BASE}/datasets/{dataset_id}/items",
        params={"format": "json", "clean": "true"},
        headers=apify_headers(), timeout=60
    )
    r.raise_for_status()
    return r.json()


# ── Normalise ─────────────────────────────────────────────────────────────
def normalise_profile(raw: dict) -> dict:
    followers = raw.get("followersCount", 0)
    if isinstance(followers, str):
        followers = int("".join(ch for ch in followers if ch.isdigit()) or 0)
    return {
        "username":       raw.get("username", INSTAGRAM_USER),
        "followersCount": followers,
    }


TYPE_MAP = {
    "GraphSidecar": "Sidecar", "GraphVideo": "Video", "GraphImage": "Image",
    "sidecar": "Sidecar", "carousel": "Sidecar", "carousel_container": "Sidecar",
    "video": "Video", "clip": "Video", "reel": "Video",
    "image": "Image",
}


def normalise_post(raw: dict) -> dict:
    ts_raw = raw.get("timestamp") or raw.get("takenAtTimestamp") or ""
    ts = str(ts_raw)[:10] if ts_raw else ""

    raw_type  = raw.get("type") or raw.get("productType") or "Image"
    post_type = TYPE_MAP.get(raw_type, TYPE_MAP.get(str(raw_type).lower(), "Image"))

    caption_raw = raw.get("caption") or ""
    if isinstance(caption_raw, dict):
        caption_raw = caption_raw.get("text") or ""

    post_id = raw.get("id") or raw.get("shortCode") or raw.get("url") or ""

    return {
        "id":             post_id,
        "url":            raw.get("url") or (
                          f"https://www.instagram.com/p/{raw['shortCode']}/"
                          if raw.get("shortCode") else ""),
        "type":           post_type,
        "timestamp":      ts,
        "caption":        caption_raw[:500],
        "likesCount":     int(raw.get("likesCount") or 0),
        "commentsCount":  int(raw.get("commentsCount") or 0),
        "videoViewCount": int(raw.get("videoViewCount") or raw.get("videoPlayCount") or 0) or None,
        "displayUrl":     raw.get("displayUrl") or raw.get("thumbnailUrl") or "",
    }


def extract_raw_posts(profile_raw: dict) -> list:
    for key in RAW_POSTS_KEYS:
        if profile_raw.get(key):
            log.info(f"  found embedded posts under key '{key}'")
            return profile_raw[key]
    log.warning(
        "No embedded posts found under any of %s. "
        "Available top-level keys in the response: %s",
        RAW_POSTS_KEYS, list(profile_raw.keys()),
    )
    return []


# ── Merge with existing data ────────────────────────────────────────────────
def merge_posts(existing: list, fresh: list) -> list:
    """Keep growing history over time instead of only ever having the last
    N posts — 'details' mode only returns ~12-50 recent posts per run, so
    without merging, older posts would disappear from month-on-month view."""
    by_id = {p.get("id") or p.get("url"): p for p in existing if p.get("id") or p.get("url")}
    for p in fresh:
        key = p.get("id") or p.get("url")
        if key:
            by_id[key] = p
    merged = [p for p in by_id.values() if p.get("timestamp")]
    merged.sort(key=lambda p: p["timestamp"], reverse=True)
    return merged


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    log.info(f"Starting scrape for @{INSTAGRAM_USER}  ({datetime.now(timezone.utc).isoformat()})")

    run_id     = start_scraper()
    dataset_id = wait_for_run(run_id)
    items      = fetch_dataset(dataset_id)

    if not items:
        raise ValueError("instagram-scraper returned no items")

    profile_raw = items[0]
    profile     = normalise_profile(profile_raw)

    raw_posts   = extract_raw_posts(profile_raw)
    fresh_posts = [normalise_post(p) for p in raw_posts]
    log.info(f"  {len(fresh_posts)} posts parsed from this run")

    existing_posts = []
    if OUTPUT_PATH.exists():
        try:
            with open(OUTPUT_PATH) as f:
                old = json.load(f)
            existing_posts = old.get("posts", [])
            log.info(f"  {len(existing_posts)} posts in existing history")
        except Exception as e:
            log.warning(f"Could not read existing JSON: {e}")

    merged_posts = merge_posts(existing_posts, fresh_posts)
    log.info(f"  {len(merged_posts)} posts total after merge")

    output = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "profile":   profile,
        "posts":     merged_posts,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    log.info(f"Written -> {OUTPUT_PATH}  ({OUTPUT_PATH.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
