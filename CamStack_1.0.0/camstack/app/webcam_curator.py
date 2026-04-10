"""webcam_curator.py — nature webcam discovery, curation, and recommendation engine.

Discovers real webcams from multiple sources, tracks playback/failure events,
and recommends feeds weighted toward variety and reliability.

Mounted on the CamStack FastAPI app at /api/curator/.

Discovery sources:
  - seeds        : Built-in curated list of known-good nature webcam channels
  - windy        : Windy.com webcam API (requires free API key in config.json)
  - nps          : US National Park Service API (requires free API key)
  - skyline      : SkylineWebcams nature/wildlife/marine sections (scraped)
  - earthcam     : EarthCam nature/animals/parks sections (scraped)
  - alertwildfire: AlertCalifornia fire-watch wilderness cameras

Configuration (add to runtime/config.json under "curator"):
  {
    "curator": {
      "windy_api_key": "",
      "nps_api_key": "",
      "discovery_interval_hours": 24,
      "min_reliability_threshold": 0.2
    }
  }
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional

import requests
from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel

# ── Paths ───────────────────────────────────────────────────────────────────
BASE = Path("/opt/camstack")
DB_PATH = BASE / "runtime" / "webcams.db"
_CFG_PATH = BASE / "runtime" / "config.json"

# ── Curator config defaults (stored under "curator" key in config.json) ─────
_DEFAULT_CFG: dict = {
    "windy_api_key": "",
    "nps_api_key": "",
    "discovery_interval_hours": 24,
    "min_reliability_threshold": 0.2,
}

# ── Single write lock — SQLite WAL handles concurrent reads fine ─────────────
_db_lock = threading.Lock()

# ── Schema ───────────────────────────────────────────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS feeds (
    id            TEXT PRIMARY KEY,
    url           TEXT UNIQUE NOT NULL,
    title         TEXT    NOT NULL DEFAULT '',
    source        TEXT    NOT NULL DEFAULT 'manual',
    category      TEXT    NOT NULL DEFAULT 'nature',
    tags          TEXT    NOT NULL DEFAULT '[]',
    location      TEXT    NOT NULL DEFAULT '{}',
    thumbnail     TEXT    NOT NULL DEFAULT '',
    added_at      INTEGER NOT NULL,
    last_verified INTEGER,
    active        INTEGER NOT NULL DEFAULT 1,
    blocked       INTEGER NOT NULL DEFAULT 0,
    notes         TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_id    TEXT    NOT NULL,
    event_type TEXT    NOT NULL,
    ts         INTEGER NOT NULL,
    duration   INTEGER,
    detail     TEXT,
    FOREIGN KEY (feed_id) REFERENCES feeds(id)
);

CREATE TABLE IF NOT EXISTS feed_stats (
    feed_id        TEXT    PRIMARY KEY,
    play_count     INTEGER NOT NULL DEFAULT 0,
    fail_count     INTEGER NOT NULL DEFAULT 0,
    skip_count     INTEGER NOT NULL DEFAULT 0,
    reject_count   INTEGER NOT NULL DEFAULT 0,
    total_duration INTEGER NOT NULL DEFAULT 0,
    last_played    INTEGER,
    last_failed    INTEGER,
    score          REAL    NOT NULL DEFAULT 0.5,
    FOREIGN KEY (feed_id) REFERENCES feeds(id)
);

CREATE TABLE IF NOT EXISTS blocklist (
    word       TEXT    PRIMARY KEY,
    added_at   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_feed   ON events(feed_id);
CREATE INDEX IF NOT EXISTS idx_events_ts     ON events(ts);
CREATE INDEX IF NOT EXISTS idx_feeds_active  ON feeds(active, blocked);
"""


# ── DB helpers ───────────────────────────────────────────────────────────────

@contextmanager
def _get_db() -> Generator[sqlite3.Connection, None, None]:
    """Thread-safe SQLite connection with automatic init, commit, rollback."""
    with _db_lock:
        conn = sqlite3.connect(str(DB_PATH), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            conn.executescript(_SCHEMA)
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _feed_id(url: str) -> str:
    """Stable 16-char hex ID derived from the normalised URL."""
    return hashlib.sha1(url.strip().lower().encode()).hexdigest()[:16]


def _load_curator_cfg() -> dict:
    try:
        raw = json.loads(_CFG_PATH.read_text()) if _CFG_PATH.exists() else {}
        return {**_DEFAULT_CFG, **raw.get("curator", {})}
    except Exception:
        return dict(_DEFAULT_CFG)


# ── Blocklist ────────────────────────────────────────────────────────────────

def _get_blocklist_words(conn: sqlite3.Connection) -> set[str]:
    return {r["word"].lower() for r in conn.execute("SELECT word FROM blocklist").fetchall()}


def _title_blocked(title: str, blocked_words: set[str]) -> bool:
    if not title or not blocked_words:
        return False
    words = set(re.findall(r"[a-z]+", title.lower()))
    return bool(words & blocked_words)


# ── Feed upsert ──────────────────────────────────────────────────────────────

def _upsert_feed(conn: sqlite3.Connection, feed: dict) -> bool:
    """Insert a new feed; no-op if URL already exists. Returns True if inserted."""
    fid = _feed_id(feed["url"])
    if conn.execute("SELECT id FROM feeds WHERE id=?", (fid,)).fetchone():
        return False
    conn.execute(
        """INSERT INTO feeds
               (id, url, title, source, category, tags, location, thumbnail, added_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            fid,
            feed["url"].strip(),
            feed.get("title") or "",
            feed.get("source") or "manual",
            feed.get("category") or "nature",
            json.dumps(feed.get("tags") or []),
            json.dumps(feed.get("location") or {}),
            feed.get("thumbnail") or "",
            int(time.time()),
        ),
    )
    conn.execute("INSERT OR IGNORE INTO feed_stats (feed_id) VALUES (?)", (fid,))
    return True


# ── Discovery: seed list ─────────────────────────────────────────────────────
# Known-good public nature webcam channels and direct streams.
# YouTube /live pages are resolved at play-time via yt-dlp in player.py.

_SEED_FEEDS: list[dict] = [
    # ── Explore.org / Cornell Lab ──────────────────────────────────────────
    {"url": "https://www.youtube.com/@ExploreOrg/live",
     "title": "Explore.org Live Cams",          "source": "explore",  "category": "wildlife",
     "tags": ["bears", "eagles", "wolves", "birds"]},
    {"url": "https://www.youtube.com/@CornellLabBirdCams/live",
     "title": "Cornell Lab Bird Cams",           "source": "explore",  "category": "birds",
     "tags": ["birds", "nest", "feeder"]},
    {"url": "https://www.youtube.com/@Africam/live",
     "title": "Africam — African Reserve",       "source": "explore",  "category": "wildlife",
     "tags": ["africa", "safari", "watering-hole"]},
    {"url": "https://www.youtube.com/@AfricanWildlifeFdn/live",
     "title": "African Wildlife Foundation",     "source": "awf",      "category": "wildlife",
     "tags": ["africa"]},

    # ── National Parks ─────────────────────────────────────────────────────
    {"url": "https://www.youtube.com/@YellowstoneNPS/live",
     "title": "Yellowstone National Park",       "source": "nps",      "category": "landscape",
     "tags": ["yellowstone", "geysers", "bison"]},

    # ── Oceans & marine ───────────────────────────────────────────────────
    {"url": "https://www.youtube.com/@OceanExplorationTrust/live",
     "title": "Ocean Exploration Trust",         "source": "noaa",     "category": "marine",
     "tags": ["deep-sea", "ocean"]},
    {"url": "https://www.youtube.com/@NOAAOceanExplorer/live",
     "title": "NOAA Ocean Explorer",             "source": "noaa",     "category": "marine",
     "tags": ["ocean", "noaa"]},

    # ── Nordic / landscape ─────────────────────────────────────────────────
    {"url": "https://www.youtube.com/@VisitNorway/live",
     "title": "Visit Norway Live",               "source": "visitnorway", "category": "landscape",
     "tags": ["norway", "fjords", "northern-lights"]},
    {"url": "https://www.youtube.com/@NorskNatur/live",
     "title": "Norsk Natur",                     "source": "norsknatur", "category": "wildlife",
     "tags": ["norway", "wildlife"]},

    # ── Wildlife BBC / WildEarth ────────────────────────────────────────────
    {"url": "https://www.youtube.com/@BBCEarth/live",
     "title": "BBC Earth Live",                  "source": "bbc",      "category": "wildlife"},
    {"url": "https://www.youtube.com/@WildEarth/live",
     "title": "WildEarth Live",                  "source": "wildearth","category": "wildlife",
     "tags": ["africa", "safari"]},

    # ── Zoos ──────────────────────────────────────────────────────────────
    {"url": "https://www.youtube.com/@SanDiegoZoo/live",
     "title": "San Diego Zoo",                   "source": "zoo",      "category": "wildlife",
     "tags": ["zoo"]},
    {"url": "https://www.youtube.com/@SmithsonianNationalZoo/live",
     "title": "Smithsonian National Zoo",        "source": "zoo",      "category": "wildlife",
     "tags": ["zoo"]},
    {"url": "https://www.youtube.com/@Cincinnati_Zoo/live",
     "title": "Cincinnati Zoo",                  "source": "zoo",      "category": "wildlife",
     "tags": ["zoo"]},

    # ── Audubon / birds ────────────────────────────────────────────────────
    {"url": "https://www.youtube.com/@AudubonSociety/live",
     "title": "Audubon Society",                 "source": "audubon",  "category": "birds",
     "tags": ["birds"]},

    # ── Relaxation / landscape ─────────────────────────────────────────────
    {"url": "https://www.youtube.com/@NatureRelaxation/live",
     "title": "Nature Relaxation",               "source": "youtube",  "category": "landscape"},
    {"url": "https://www.youtube.com/@EarthCam/live",
     "title": "EarthCam Live",                   "source": "earthcam", "category": "landscape"},
]


def discover_seeds() -> list[dict]:
    return list(_SEED_FEEDS)


# ── Discovery: Windy.com ─────────────────────────────────────────────────────

def discover_windy(api_key: str, limit: int = 50) -> list[dict]:
    """
    Windy.com Webcam API v3 — thousands of real outdoor webcams worldwide.
    Free API key: https://api.windy.com/
    """
    if not api_key:
        logger.debug("[Curator] Windy API key not configured — skipping")
        return []

    results: list[dict] = []
    seen: set[str] = set()
    categories = ["nature", "animals", "mountains", "seas", "coast", "lakes", "rivers"]
    headers = {"x-windy-api-key": api_key}

    for cat in categories:
        try:
            resp = requests.get(
                "https://api.windy.com/webcams/api/v3/webcams",
                params={"limit": limit, "offset": 0, "category": cat,
                        "include": "player,location", "lang": "en"},
                headers=headers,
                timeout=15,
            )
            if resp.status_code != 200:
                logger.warning(f"[Curator] Windy API {cat!r}: HTTP {resp.status_code}")
                continue
            for wc in resp.json().get("webcams", []):
                player = wc.get("player", {})
                url = (player.get("day", {}).get("embed")
                       or player.get("live", {}).get("embed"))
                if not url or url in seen:
                    continue
                seen.add(url)
                loc = wc.get("location", {})
                results.append({
                    "url": url,
                    "title": wc.get("title") or "",
                    "source": "windy",
                    "category": cat,
                    "tags": [cat],
                    "location": {
                        "lat": loc.get("latitude"),
                        "lon": loc.get("longitude"),
                        "country": loc.get("country"),
                        "region": loc.get("region"),
                    },
                    "thumbnail": wc.get("image", {}).get("current", {}).get("preview") or "",
                })
        except Exception as e:
            logger.warning(f"[Curator] Windy discovery failed for {cat!r}: {e}")

    logger.info(f"[Curator] Windy: found {len(results)} feeds")
    return results


# ── Discovery: SkylineWebcams ────────────────────────────────────────────────

def discover_skylinewebcams(max_per_section: int = 30) -> list[dict]:
    """
    Scrape SkylineWebcams nature, wildlife, marine, and mountain sections.
    Returns embed page URLs — the player resolves these at stream time.
    """
    sections = [
        ("https://www.skylinewebcams.com/en/webcam/natura.html",   "nature"),
        ("https://www.skylinewebcams.com/en/webcam/animali.html",   "wildlife"),
        ("https://www.skylinewebcams.com/en/webcam/mare.html",      "marine"),
        ("https://www.skylinewebcams.com/en/webcam/montagna.html",  "landscape"),
    ]
    results: list[dict] = []
    seen: set[str] = set()
    headers = {"User-Agent": "CamStack/2.0 webcam-curator (open source)"}

    for page_url, category in sections:
        try:
            resp = requests.get(page_url, headers=headers, timeout=12)
            if resp.status_code != 200:
                logger.warning(f"[Curator] SkylineWebcams {page_url}: HTTP {resp.status_code}")
                continue

            # Extract embedded cam IDs from anchor hrefs
            cam_ids = re.findall(
                r'href="https://www\.skylinewebcams\.com/[a-z]+/webcam/[^"]+/([^/"]+)\.html"',
                resp.text,
            )
            # Also catch data-id attributes
            cam_ids += re.findall(r'data-id=["\'](\d+)["\']', resp.text)
            cam_ids = list(dict.fromkeys(cam_ids))[:max_per_section]

            # Grab page titles from heading tags near cam links
            raw_titles = re.findall(
                r'<(?:h[1-4]|span)[^>]*class="[^"]*(?:title|name)[^"]*"[^>]*>([^<]{3,80})</',
                resp.text,
            )
            titles = [t.strip() for t in raw_titles]

            for i, cid in enumerate(cam_ids):
                url = f"https://embed.skylinewebcams.com/cam/{cid}.html"
                if url in seen:
                    continue
                seen.add(url)
                results.append({
                    "url": url,
                    "title": titles[i] if i < len(titles) else f"SkylineWebcam {cid}",
                    "source": "skylinewebcams",
                    "category": category,
                    "tags": [category],
                })
        except Exception as e:
            logger.warning(f"[Curator] SkylineWebcams scrape failed for {page_url}: {e}")

    logger.info(f"[Curator] SkylineWebcams: found {len(results)} feeds")
    return results


# ── Discovery: EarthCam ──────────────────────────────────────────────────────

def discover_earthcam() -> list[dict]:
    """
    Scrape EarthCam's nature, animals, and national park sections.
    Extracts YouTube embeds and direct cam page URLs.
    """
    sections = [
        ("https://www.earthcam.com/nature/",     "nature"),
        ("https://www.earthcam.com/animals/",    "wildlife"),
        ("https://www.earthcam.com/usa/parks/",  "landscape"),
    ]
    results: list[dict] = []
    seen: set[str] = set()
    headers = {"User-Agent": "CamStack/2.0 webcam-curator (open source)"}

    for page_url, category in sections:
        try:
            resp = requests.get(page_url, headers=headers, timeout=12)
            if resp.status_code != 200:
                logger.warning(f"[Curator] EarthCam {page_url}: HTTP {resp.status_code}")
                continue

            # Direct cam pages
            for path in re.findall(r'href="(https://www\.earthcam\.com/cams/[^"]+)"', resp.text)[:20]:
                if path not in seen:
                    seen.add(path)
                    results.append({"url": path, "title": "", "source": "earthcam", "category": category})

            # YouTube video embeds
            title_tags = re.findall(
                r'<(?:h\d|div)[^>]*class="[^"]*(?:title|cam.?name)[^"]*"[^>]*>([^<]{3,80})</',
                resp.text,
            )
            titles = [t.strip() for t in title_tags]
            for i, vid in enumerate(re.findall(r'youtube\.com/embed/([A-Za-z0-9_-]{11})', resp.text)[:20]):
                url = f"https://www.youtube.com/watch?v={vid}"
                if url not in seen:
                    seen.add(url)
                    results.append({
                        "url": url,
                        "title": titles[i] if i < len(titles) else "",
                        "source": "earthcam",
                        "category": category,
                    })
        except Exception as e:
            logger.warning(f"[Curator] EarthCam scrape failed for {page_url}: {e}")

    logger.info(f"[Curator] EarthCam: found {len(results)} feeds")
    return results


# ── Discovery: NPS ───────────────────────────────────────────────────────────

def discover_nps(api_key: str = "") -> list[dict]:
    """
    US National Park Service webcams.
    Free API key: https://www.nps.gov/subjects/developer/
    Falls back to scraping the NPS webcams HTML page when no key is provided.
    """
    results: list[dict] = []
    headers = {"User-Agent": "CamStack/2.0 webcam-curator (open source)"}

    if api_key:
        try:
            resp = requests.get(
                "https://developer.nps.gov/api/v1/webcams",
                params={"api_key": api_key, "limit": 200},
                headers=headers,
                timeout=15,
            )
            if resp.status_code == 200:
                for cam in resp.json().get("data", []):
                    url = cam.get("url") or cam.get("streamUrl") or cam.get("embedUrl")
                    if url:
                        results.append({
                            "url": url,
                            "title": cam.get("title") or cam.get("name") or "",
                            "source": "nps",
                            "category": "landscape",
                            "tags": ["national-park"],
                            "location": {"country": "US", "region": cam.get("parkCode", "")},
                        })
                logger.info(f"[Curator] NPS API: found {len(results)} feeds")
                return results
            logger.warning(f"[Curator] NPS API: HTTP {resp.status_code}")
        except Exception as e:
            logger.warning(f"[Curator] NPS API failed: {e}")

    # Fallback: scrape the public webcams page for YouTube embeds
    try:
        resp = requests.get(
            "https://www.nps.gov/subjects/digital/nps-webcams.htm",
            headers=headers, timeout=15,
        )
        if resp.status_code == 200:
            yt_ids = re.findall(r'youtube\.com/embed/([A-Za-z0-9_-]{11})', resp.text)
            yt_ids += re.findall(r'youtu\.be/([A-Za-z0-9_-]{11})', resp.text)
            for vid in dict.fromkeys(yt_ids):
                results.append({
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "title": "",
                    "source": "nps",
                    "category": "landscape",
                    "tags": ["national-park"],
                })
    except Exception as e:
        logger.warning(f"[Curator] NPS scrape failed: {e}")

    logger.info(f"[Curator] NPS: found {len(results)} feeds")
    return results


# ── Discovery: AlertWildfire / AlertCalifornia ───────────────────────────────

def discover_alertwildfire() -> list[dict]:
    """
    AlertCalifornia fire-watch cameras — remote mountain, ridge, and wilderness views.
    These are real fixed cameras with spectacular landscape imagery.
    """
    results: list[dict] = []
    headers = {"User-Agent": "CamStack/2.0 webcam-curator (open source)"}

    # AlertCalifornia publishes a CORS-accessible JSON camera list
    try:
        resp = requests.get(
            "https://cameras.alertcalifornia.org/public-cameras.json",
            headers=headers, timeout=15,
        )
        if resp.status_code == 200:
            for cam in resp.json():
                streams = cam.get("streams") or []
                url = next((s["url"] for s in streams if s.get("url")), None)
                if not url:
                    url = cam.get("url") or cam.get("streamUrl")
                if not url:
                    continue
                loc = cam.get("location") or {}
                results.append({
                    "url": url,
                    "title": cam.get("name") or cam.get("id") or "",
                    "source": "alertwildfire",
                    "category": "landscape",
                    "tags": ["wilderness", "mountains", "fire-watch", "california"],
                    "location": {
                        "lat": loc.get("latitude") or loc.get("lat"),
                        "lon": loc.get("longitude") or loc.get("lon"),
                        "country": "US",
                        "region": "California",
                    },
                })
        else:
            logger.warning(f"[Curator] AlertCalifornia: HTTP {resp.status_code}")
    except Exception as e:
        logger.warning(f"[Curator] AlertCalifornia failed: {e}")

    logger.info(f"[Curator] AlertWildfire: found {len(results)} feeds")
    return results


# ── Master discovery runner ──────────────────────────────────────────────────

def run_discovery() -> dict:
    """Run all discovery sources and upsert new feeds into the database."""
    cfg = _load_curator_cfg()
    logger.info("[Curator] Starting discovery run")

    found: list[dict] = []
    found += discover_seeds()
    found += discover_windy(cfg.get("windy_api_key", ""))
    found += discover_skylinewebcams()
    found += discover_earthcam()
    found += discover_nps(cfg.get("nps_api_key", ""))
    found += discover_alertwildfire()

    inserted = 0
    blocked_count = 0
    with _get_db() as conn:
        blocked_words = _get_blocklist_words(conn)
        for feed in found:
            if _title_blocked(feed.get("title", ""), blocked_words):
                blocked_count += 1
                continue
            if _upsert_feed(conn, feed):
                inserted += 1

    logger.info(
        f"[Curator] Discovery complete — {inserted} new feeds added, "
        f"{blocked_count} blocked by word filter, {len(found)} total found"
    )
    return {"inserted": inserted, "blocked": blocked_count, "total_found": len(found)}


# ── Recommendation engine ─────────────────────────────────────────────────────

def _compute_score(stats: sqlite3.Row, now: int) -> float:
    """
    Score a feed for weighted-random recommendation.

    reliability = plays / (plays + fails + skips)  [Laplace-smoothed; 0.5 for new]
    novelty     = how long since last played (saturates at 1.0 after 24 h)

    Final score = reliability * 0.35 + novelty * 0.65
    Novelty gets higher weight so the system naturally rotates through the catalogue.
    """
    plays = stats["play_count"] or 0
    fails = stats["fail_count"] or 0
    skips = stats["skip_count"] or 0
    total = plays + fails + skips
    reliability = (plays + 1) / (total + 2)          # Laplace smoothing

    last_played = stats["last_played"] or 0
    hours_ago = max(0, (now - last_played) / 3600) if last_played else 999
    novelty = min(1.0, hours_ago / 24.0)

    return reliability * 0.35 + novelty * 0.65


def recommend(n: int = 1, exclude: list[str] | None = None) -> list[dict]:
    """
    Return up to n recommended feed dicts, using weighted random selection.
    exclude: list of feed URLs to skip (e.g. currently playing).
    """
    excluded = set(exclude or [])
    now = int(time.time())

    with _get_db() as conn:
        rows = conn.execute(
            """SELECT f.id, f.url, f.title, f.source, f.category, f.tags, f.location,
                      f.thumbnail,
                      COALESCE(s.play_count,0)  AS play_count,
                      COALESCE(s.fail_count,0)  AS fail_count,
                      COALESCE(s.skip_count,0)  AS skip_count,
                      s.last_played
               FROM feeds f
               LEFT JOIN feed_stats s ON f.id = s.feed_id
               WHERE f.active=1 AND f.blocked=0"""
        ).fetchall()

    candidates = [r for r in rows if r["url"] not in excluded]
    if not candidates:
        return []

    weights = [_compute_score(r, now) for r in candidates]
    total_w = sum(weights) or float(len(candidates))
    if total_w == 0:
        weights = [1.0] * len(candidates)
        total_w = float(len(candidates))

    picks: list[dict] = []
    pool = list(zip(candidates, weights))

    for _ in range(min(n, len(pool))):
        tw = sum(w for _, w in pool)
        r_val = random.random() * tw
        cumulative = 0.0
        chosen = 0
        for idx, (_, w) in enumerate(pool):
            cumulative += w
            if r_val <= cumulative:
                chosen = idx
                break
        row, _ = pool.pop(chosen)
        picks.append({
            "id":           row["id"],
            "url":          row["url"],
            "title":        row["title"],
            "source":       row["source"],
            "category":     row["category"],
            "tags":         json.loads(row["tags"] or "[]"),
            "location":     json.loads(row["location"] or "{}"),
            "thumbnail":    row["thumbnail"],
            "play_count":   row["play_count"],
            "fail_count":   row["fail_count"],
            "last_played":  row["last_played"],
        })

    return picks


# ── Event recording ──────────────────────────────────────────────────────────

def record_event(
    feed_url: str,
    event_type: str,
    duration: int | None = None,
    detail: str | None = None,
) -> bool:
    """
    Record a playback event.
    event_type: 'played' | 'failed' | 'skipped' | 'rejected'
    Returns True if the feed was found and the event recorded.
    """
    fid = _feed_id(feed_url)
    now = int(time.time())

    with _get_db() as conn:
        if not conn.execute("SELECT id FROM feeds WHERE id=?", (fid,)).fetchone():
            logger.warning(f"[Curator] record_event: unknown feed URL — {feed_url!r}")
            return False

        conn.execute(
            "INSERT INTO events (feed_id, event_type, ts, duration, detail) VALUES (?,?,?,?,?)",
            (fid, event_type, now, duration, detail),
        )
        conn.execute("INSERT OR IGNORE INTO feed_stats (feed_id) VALUES (?)", (fid,))

        if event_type == "played":
            conn.execute(
                """UPDATE feed_stats
                   SET play_count = play_count + 1,
                       total_duration = total_duration + COALESCE(?,0),
                       last_played = ?
                   WHERE feed_id=?""",
                (duration or 0, now, fid),
            )
        elif event_type == "failed":
            conn.execute(
                "UPDATE feed_stats SET fail_count=fail_count+1, last_failed=? WHERE feed_id=?",
                (now, fid),
            )
            _maybe_retire(conn, fid)
        elif event_type == "skipped":
            conn.execute("UPDATE feed_stats SET skip_count=skip_count+1 WHERE feed_id=?", (fid,))
        elif event_type == "rejected":
            conn.execute("UPDATE feed_stats SET reject_count=reject_count+1 WHERE feed_id=?", (fid,))

        # Recompute and persist score
        stats = conn.execute("SELECT * FROM feed_stats WHERE feed_id=?", (fid,)).fetchone()
        if stats:
            conn.execute(
                "UPDATE feed_stats SET score=? WHERE feed_id=?",
                (_compute_score(stats, now), fid),
            )

    return True


def _maybe_retire(conn: sqlite3.Connection, feed_id: str) -> None:
    """Deactivate a chronically failing feed when its reliability falls below threshold."""
    cfg = _load_curator_cfg()
    threshold = float(cfg.get("min_reliability_threshold", 0.2))
    stats = conn.execute(
        "SELECT play_count, fail_count FROM feed_stats WHERE feed_id=?", (feed_id,)
    ).fetchone()
    if not stats:
        return
    plays, fails = stats["play_count"], stats["fail_count"]
    total = plays + fails
    if total >= 10 and plays / total < threshold:
        conn.execute("UPDATE feeds SET active=0 WHERE id=?", (feed_id,))
        logger.warning(
            f"[Curator] Feed {feed_id} retired — reliability {plays}/{total} "
            f"({100*plays//total}%) below {int(threshold*100)}% threshold"
        )


# ── FastAPI router ────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/curator", tags=["curator"])


class FeedAdd(BaseModel):
    url: str
    title: str = ""
    source: str = "manual"
    category: str = "nature"
    tags: list[str] = []
    notes: str = ""


class EventReport(BaseModel):
    feed_url: str
    event_type: str     # played | failed | skipped | rejected
    duration: Optional[int] = None
    detail: Optional[str] = None


class BlocklistWord(BaseModel):
    word: str


# ── Feed endpoints ────────────────────────────────────────────────────────────

@router.get("/feeds/recommend")
def api_recommend(n: int = 1, exclude: str = ""):
    """
    Get n recommended feed(s), weighted toward variety and reliability.
    exclude: comma-separated list of URLs to skip (e.g. currently playing).
    """
    excluded = [u.strip() for u in exclude.split(",") if u.strip()]
    feeds = recommend(n=n, exclude=excluded)
    return JSONResponse({"feeds": feeds, "count": len(feeds)})


@router.get("/feeds")
def api_list_feeds(
    source: str = "",
    category: str = "",
    active_only: bool = True,
    limit: int = 200,
    offset: int = 0,
):
    """List feeds with their stats, sorted by score descending."""
    with _get_db() as conn:
        query = """
            SELECT f.id, f.url, f.title, f.source, f.category, f.tags, f.location,
                   f.thumbnail, f.active, f.blocked, f.added_at, f.notes,
                   COALESCE(s.play_count,0)     AS play_count,
                   COALESCE(s.fail_count,0)     AS fail_count,
                   COALESCE(s.skip_count,0)     AS skip_count,
                   COALESCE(s.reject_count,0)   AS reject_count,
                   COALESCE(s.total_duration,0) AS total_duration,
                   s.last_played, s.last_failed,
                   COALESCE(s.score, 0.5)       AS score
            FROM feeds f
            LEFT JOIN feed_stats s ON f.id = s.feed_id
            WHERE 1=1
        """
        params: list = []
        if active_only:
            query += " AND f.active=1 AND f.blocked=0"
        if source:
            query += " AND f.source=?"
            params.append(source)
        if category:
            query += " AND f.category=?"
            params.append(category)
        query += " ORDER BY COALESCE(s.score, 0.5) DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        rows = conn.execute(query, params).fetchall()
        feeds = [dict(r) for r in rows]
    return JSONResponse({"feeds": feeds, "count": len(feeds)})


@router.get("/feeds/{feed_id}")
def api_get_feed(feed_id: str):
    """Get a single feed by ID with full stats."""
    with _get_db() as conn:
        row = conn.execute(
            """SELECT f.*, COALESCE(s.play_count,0) AS play_count,
                      COALESCE(s.fail_count,0) AS fail_count,
                      COALESCE(s.skip_count,0) AS skip_count,
                      COALESCE(s.total_duration,0) AS total_duration,
                      s.last_played, s.last_failed, COALESCE(s.score,0.5) AS score
               FROM feeds f LEFT JOIN feed_stats s ON f.id=s.feed_id
               WHERE f.id=?""",
            (feed_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Feed not found")
        recent = conn.execute(
            "SELECT event_type, ts, duration, detail FROM events WHERE feed_id=? ORDER BY ts DESC LIMIT 20",
            (feed_id,),
        ).fetchall()
    result = dict(row)
    result["recent_events"] = [dict(e) for e in recent]
    return JSONResponse(result)


@router.post("/feeds")
def api_add_feed(body: FeedAdd):
    """Manually add a feed to the curator database."""
    with _get_db() as conn:
        inserted = _upsert_feed(conn, body.model_dump())
    if not inserted:
        return JSONResponse({"ok": False, "reason": "URL already exists"}, status_code=409)
    logger.info(f"[Curator] Manually added feed: {body.url!r}")
    return JSONResponse({"ok": True, "id": _feed_id(body.url)}, status_code=201)


@router.patch("/feeds/{feed_id}/block")
def api_block_feed(feed_id: str):
    """Block a feed from being recommended (soft-delete)."""
    with _get_db() as conn:
        if not conn.execute("SELECT id FROM feeds WHERE id=?", (feed_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Feed not found")
        conn.execute("UPDATE feeds SET blocked=1, active=0 WHERE id=?", (feed_id,))
    return JSONResponse({"ok": True})


@router.patch("/feeds/{feed_id}/unblock")
def api_unblock_feed(feed_id: str):
    """Re-enable a previously blocked feed."""
    with _get_db() as conn:
        if not conn.execute("SELECT id FROM feeds WHERE id=?", (feed_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Feed not found")
        conn.execute("UPDATE feeds SET blocked=0, active=1 WHERE id=?", (feed_id,))
    return JSONResponse({"ok": True})


# ── Event endpoint ────────────────────────────────────────────────────────────

@router.post("/events")
def api_record_event(body: EventReport):
    """
    Report a playback event from CamStack back to the curator.

    event_type values:
      played   — feed was displayed (include duration in seconds)
      failed   — feed failed to load / stream error
      skipped  — user skipped or dismissed the feed
      rejected — feed was rejected by a title/content filter
    """
    valid = {"played", "failed", "skipped", "rejected"}
    if body.event_type not in valid:
        raise HTTPException(status_code=400, detail=f"event_type must be one of {sorted(valid)}")
    ok = record_event(body.feed_url, body.event_type, body.duration, body.detail)
    if not ok:
        raise HTTPException(status_code=404, detail="Feed URL not found in curator database")
    return JSONResponse({"ok": True})


# ── Discovery endpoint ────────────────────────────────────────────────────────

@router.post("/discover")
def api_discover(background_tasks: BackgroundTasks):
    """
    Trigger a full discovery run in the background.
    Scans all configured sources and inserts new feeds.
    """
    background_tasks.add_task(_run_discovery_task)
    return JSONResponse({"ok": True, "message": "Discovery run started in background"})


def _run_discovery_task() -> None:
    try:
        result = run_discovery()
        logger.info(f"[Curator] Background discovery finished: {result}")
    except Exception as e:
        logger.exception(f"[Curator] Background discovery failed: {e}")


# ── Stats endpoint ────────────────────────────────────────────────────────────

@router.get("/stats")
def api_stats():
    """Overall curator database statistics."""
    with _get_db() as conn:
        total    = conn.execute("SELECT COUNT(*) FROM feeds").fetchone()[0]
        active   = conn.execute("SELECT COUNT(*) FROM feeds WHERE active=1 AND blocked=0").fetchone()[0]
        retired  = conn.execute("SELECT COUNT(*) FROM feeds WHERE active=0 AND blocked=0").fetchone()[0]
        blocked  = conn.execute("SELECT COUNT(*) FROM feeds WHERE blocked=1").fetchone()[0]
        plays    = conn.execute("SELECT COALESCE(SUM(play_count),0) FROM feed_stats").fetchone()[0]
        fails    = conn.execute("SELECT COALESCE(SUM(fail_count),0) FROM feed_stats").fetchone()[0]
        skips    = conn.execute("SELECT COALESCE(SUM(skip_count),0) FROM feed_stats").fetchone()[0]
        tot_dur  = conn.execute("SELECT COALESCE(SUM(total_duration),0) FROM feed_stats").fetchone()[0]
        sources  = conn.execute(
            "SELECT source, COUNT(*) as n FROM feeds WHERE active=1 AND blocked=0 GROUP BY source ORDER BY n DESC"
        ).fetchall()
        cats     = conn.execute(
            "SELECT category, COUNT(*) as n FROM feeds WHERE active=1 AND blocked=0 GROUP BY category ORDER BY n DESC"
        ).fetchall()
    return JSONResponse({
        "total_feeds":      total,
        "active_feeds":     active,
        "retired_feeds":    retired,
        "blocked_feeds":    blocked,
        "total_plays":      plays,
        "total_failures":   fails,
        "total_skips":      skips,
        "total_hours_played": round(tot_dur / 3600, 1),
        "by_source":        [dict(r) for r in sources],
        "by_category":      [dict(r) for r in cats],
    })


# ── Blocklist endpoints ───────────────────────────────────────────────────────

@router.get("/blocklist")
def api_get_blocklist():
    """List all words in the curator blocklist."""
    with _get_db() as conn:
        rows = conn.execute("SELECT word, added_at FROM blocklist ORDER BY word").fetchall()
    return JSONResponse({"words": [dict(r) for r in rows], "count": len(rows)})


@router.post("/blocklist")
def api_add_blocklist(body: BlocklistWord):
    """Add a word to the blocklist. Existing feeds with this word will not be retired automatically — block them individually if needed."""
    word = body.word.strip().lower()
    if not word:
        raise HTTPException(status_code=400, detail="word must not be empty")
    with _get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO blocklist (word, added_at) VALUES (?,?)",
            (word, int(time.time())),
        )
    logger.info(f"[Curator] Blocklist: added {word!r}")
    return JSONResponse({"ok": True, "word": word})


@router.delete("/blocklist/{word}")
def api_remove_blocklist(word: str):
    """Remove a word from the blocklist."""
    with _get_db() as conn:
        result = conn.execute("DELETE FROM blocklist WHERE word=?", (word.lower(),))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"Word {word!r} not in blocklist")
    return JSONResponse({"ok": True})


# ── Startup helper (called from main.py _startup) ─────────────────────────────

def curator_startup() -> None:
    """Initialise DB and seed feeds on first run; trigger discovery if DB is empty."""
    try:
        with _get_db() as conn:
            count = conn.execute("SELECT COUNT(*) FROM feeds").fetchone()[0]
        if count == 0:
            logger.info("[Curator] Empty database — running initial seed + discovery")
            threading.Thread(target=_run_discovery_task, daemon=True, name="curator-init").start()
        else:
            logger.info(f"[Curator] Database ready — {count} feeds loaded")
    except Exception as e:
        logger.exception(f"[Curator] Startup failed: {e}")
