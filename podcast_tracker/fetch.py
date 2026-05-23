"""Fetch new podcast episodes for the tracked feeds.

Reads podcasts.json, resolves Apple Podcast IDs to RSS feed URLs (cached),
fetches each feed, identifies episodes not yet in the seen-episodes cache,
and writes a pending_episodes.json with everything the agent needs to write
detailed structured notes (title, link, pub date, show notes HTML, audio URL,
optional fetched transcript page HTML).

The agent (a scheduled Claude Code session) consumes pending_episodes.json,
produces a markdown digest, commits it, and emails it. After successful
publication the agent calls mark_seen() (or runs `python fetch.py --mark-seen
<digest_path>`) to advance the cache.

Run:
    python fetch.py             # detect new episodes, write pending_episodes.json
    python fetch.py --seed      # mark every current episode as already seen (run once)
    python fetch.py --mark-seen # advance the cache after a digest has been published
"""

from __future__ import annotations

import argparse
import html
import json
import pathlib
import re
import sys
import time
from urllib.parse import urlparse

import feedparser
import httpx

ROOT = pathlib.Path(__file__).parent.resolve()
CONFIG_PATH = ROOT / "podcasts.json"
CACHE_DIR = ROOT / "cache"
FEEDS_CACHE = CACHE_DIR / "feeds.json"
SEEN_CACHE = CACHE_DIR / "seen_episodes.json"
PENDING_PATH = CACHE_DIR / "pending_episodes.json"

ITUNES_LOOKUP = "https://itunes.apple.com/lookup?id={apple_id}"
USER_AGENT = "podcast-tracker/1.0 (+https://github.com/canneyi/facebook)"
MAX_TRANSCRIPT_BYTES = 400_000  # cap fetched transcript page size
PER_FEED_NEW_LIMIT = 5  # max new episodes pulled per feed per run


def _load_json(path: pathlib.Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return default
    return default


def _save_json(path: pathlib.Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False))


def load_config() -> list[dict]:
    cfg = json.loads(CONFIG_PATH.read_text())
    return [p for p in cfg["podcasts"] if not p.get("paused")]


def resolve_feed_urls(podcasts: list[dict], client: httpx.Client) -> dict[str, str]:
    """Resolve Apple Podcast IDs to RSS feed URLs, cached on disk."""
    cache = _load_json(FEEDS_CACHE, {})
    changed = False
    for p in podcasts:
        if p.get("feed_url"):
            cache[p["apple_id"]] = p["feed_url"]
            changed = True
            continue
        if p["apple_id"] in cache:
            continue
        url = ITUNES_LOOKUP.format(apple_id=p["apple_id"])
        try:
            r = client.get(url, timeout=15)
            r.raise_for_status()
            data = r.json()
            if data.get("resultCount"):
                feed = data["results"][0].get("feedUrl")
                if feed:
                    cache[p["apple_id"]] = feed
                    changed = True
                    print(f"resolved {p['name']} -> {feed}")
                else:
                    print(f"WARN no feedUrl for {p['name']}", file=sys.stderr)
            else:
                print(f"WARN iTunes returned no results for {p['name']}", file=sys.stderr)
        except Exception as e:
            print(f"ERR resolving {p['name']}: {e}", file=sys.stderr)
    if changed:
        _save_json(FEEDS_CACHE, cache)
    return cache


def _episode_id(entry) -> str:
    return entry.get("id") or entry.get("guid") or entry.get("link") or entry.get("title", "")


def _extract_links(text: str) -> list[str]:
    if not text:
        return []
    return re.findall(r'https?://[^\s"\'<>]+', text)


def _looks_like_transcript_link(url: str) -> bool:
    u = url.lower()
    return "transcript" in u or u.endswith(".txt") or "/transcripts/" in u


def _strip_tags(html_text: str) -> str:
    if not html_text:
        return ""
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html_text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _try_fetch_transcript(entry, client: httpx.Client) -> dict | None:
    candidates: list[str] = []
    # iTunes/Apple Podcasts <podcast:transcript> via feedparser
    for key in ("podcast_transcript", "transcript", "transcripts"):
        val = entry.get(key)
        if isinstance(val, dict) and val.get("url"):
            candidates.append(val["url"])
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, dict) and item.get("url"):
                    candidates.append(item["url"])
    # Heuristic: scan links in show notes
    notes = entry.get("summary") or entry.get("description") or ""
    for u in _extract_links(notes):
        if _looks_like_transcript_link(u):
            candidates.append(u)
    # De-dup, preserve order
    seen = set()
    uniq = []
    for u in candidates:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    for url in uniq[:3]:
        try:
            r = client.get(url, timeout=20, follow_redirects=True)
            if r.status_code != 200:
                continue
            ctype = r.headers.get("content-type", "").lower()
            body = r.text[:MAX_TRANSCRIPT_BYTES]
            text = body if "text/plain" in ctype or url.endswith(".txt") else _strip_tags(body)
            if len(text) < 400:
                continue
            return {"source_url": url, "content_type": ctype, "text": text[:MAX_TRANSCRIPT_BYTES]}
        except Exception as e:
            print(f"  transcript fetch failed {url}: {e}", file=sys.stderr)
    # Fallback: try the episode page itself for a transcript section
    page = entry.get("link")
    if page:
        try:
            r = client.get(page, timeout=20, follow_redirects=True)
            if r.status_code == 200:
                body = r.text[:MAX_TRANSCRIPT_BYTES]
                if re.search(r"transcript", body, re.IGNORECASE):
                    text = _strip_tags(body)
                    if len(text) > 1500:
                        return {"source_url": page, "content_type": "text/html", "text": text[:MAX_TRANSCRIPT_BYTES], "scraped_page": True}
        except Exception as e:
            print(f"  page fetch failed {page}: {e}", file=sys.stderr)
    return None


def _entry_audio_url(entry) -> str | None:
    for enc in entry.get("enclosures", []) or []:
        href = enc.get("href") or enc.get("url")
        if href:
            return href
    return None


def fetch_new_episodes() -> dict:
    podcasts = load_config()
    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(headers=headers, follow_redirects=True) as client:
        feeds = resolve_feed_urls(podcasts, client)
        seen = _load_json(SEEN_CACHE, {})
        result = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "episodes": []}
        for p in podcasts:
            feed_url = feeds.get(p["apple_id"])
            if not feed_url:
                print(f"SKIP {p['name']}: no feed_url", file=sys.stderr)
                continue
            print(f"\n== {p['name']}")
            try:
                # feedparser can read URLs directly but we route through httpx so the
                # UA / allowlist behaviour is consistent
                resp = client.get(feed_url, timeout=30)
                resp.raise_for_status()
                parsed = feedparser.parse(resp.content)
            except Exception as e:
                print(f"ERR fetching feed: {e}", file=sys.stderr)
                continue
            entries = parsed.entries or []
            seen_ids = set(seen.get(p["apple_id"], []))
            new_for_feed = []
            for entry in entries:
                eid = _episode_id(entry)
                if not eid or eid in seen_ids:
                    continue
                new_for_feed.append(entry)
                if len(new_for_feed) >= PER_FEED_NEW_LIMIT:
                    break
            # Process newest-first (RSS is already newest-first by convention)
            for entry in new_for_feed:
                eid = _episode_id(entry)
                show_notes_html = entry.get("content", [{}])[0].get("value") if entry.get("content") else (entry.get("summary") or entry.get("description") or "")
                show_notes_text = _strip_tags(show_notes_html)
                transcript = _try_fetch_transcript(entry, client)
                episode = {
                    "podcast": p["name"],
                    "apple_id": p["apple_id"],
                    "episode_id": eid,
                    "title": entry.get("title"),
                    "link": entry.get("link"),
                    "published": entry.get("published") or entry.get("updated"),
                    "duration": entry.get("itunes_duration"),
                    "audio_url": _entry_audio_url(entry),
                    "show_notes_html": show_notes_html,
                    "show_notes_text": show_notes_text,
                    "transcript": transcript,
                }
                result["episodes"].append(episode)
                print(f"  + {entry.get('title')} ({'transcript' if transcript else 'notes-only'})")
        _save_json(PENDING_PATH, result)
        print(f"\nWrote {PENDING_PATH} with {len(result['episodes'])} new episodes")
        return result


def seed_seen_cache() -> None:
    """Mark every currently-published episode as already seen.

    Run this once after first setup so the next real run only picks up
    episodes published after the seed.
    """
    podcasts = load_config()
    headers = {"User-Agent": USER_AGENT}
    seen: dict[str, list[str]] = {}
    with httpx.Client(headers=headers, follow_redirects=True) as client:
        feeds = resolve_feed_urls(podcasts, client)
        for p in podcasts:
            feed_url = feeds.get(p["apple_id"])
            if not feed_url:
                continue
            try:
                resp = client.get(feed_url, timeout=30)
                resp.raise_for_status()
                parsed = feedparser.parse(resp.content)
            except Exception as e:
                print(f"ERR seeding {p['name']}: {e}", file=sys.stderr)
                continue
            ids = [_episode_id(e) for e in (parsed.entries or [])]
            seen[p["apple_id"]] = [i for i in ids if i]
            print(f"seeded {p['name']}: {len(seen[p['apple_id']])} episodes")
    _save_json(SEEN_CACHE, seen)


def mark_seen() -> None:
    """Advance the seen cache to include every episode currently in pending."""
    pending = _load_json(PENDING_PATH, {"episodes": []})
    seen = _load_json(SEEN_CACHE, {})
    for ep in pending.get("episodes", []):
        ids = seen.setdefault(ep["apple_id"], [])
        if ep["episode_id"] not in ids:
            ids.append(ep["episode_id"])
    _save_json(SEEN_CACHE, seen)
    print(f"Advanced seen cache for {len(pending.get('episodes', []))} episodes")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", action="store_true", help="Seed seen-episodes cache with every currently-published episode")
    parser.add_argument("--mark-seen", action="store_true", help="Advance seen-episodes cache after a digest is published")
    args = parser.parse_args()
    if args.seed:
        seed_seen_cache()
        return 0
    if args.mark_seen:
        mark_seen()
        return 0
    fetch_new_episodes()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
