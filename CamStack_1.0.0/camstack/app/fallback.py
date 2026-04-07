from __future__ import annotations
from dataclasses import dataclass
import json
import random
import re
import subprocess
import time
from pathlib import Path
from typing import Iterable

import requests
from loguru import logger

# Curated list of reliable 24/7 nature & wildlife live streams.
# All are YouTube channel /live pages which yt-dlp can resolve to the
# current live broadcast automatically.
EXPLORE_LIVE_URLS = [
    # ── Broad nature / wildlife ──
    "https://www.youtube.com/@BBCEarth/live",
    "https://www.youtube.com/@WildEarth/live",
    # @NatGeo/live and @AnimalPlanet/live removed — both regularly air reality/cops TV, not nature.
    "https://www.youtube.com/@ExploreOrg/live",          # 100+ live cams (bears, eagles, wolves)
    "https://www.youtube.com/@Africam/live",             # African game-reserve cams
    "https://www.youtube.com/@AfricanWildlifeFdn/live",

    # ── Bird cams ──
    "https://www.youtube.com/@CornellLabBirdCams/live",  # feeder & nest cams, very stable
    "https://www.youtube.com/@AudubonSociety/live",

    # ── Zoo & aquarium (nature-only) ──
    "https://www.youtube.com/@SanDiegoZoo/live",
    "https://www.youtube.com/@SmithsonianNationalZoo/live",
    "https://www.youtube.com/@Cincinnati_Zoo/live",

    # ── National parks & landscapes ──
    "https://www.youtube.com/@YellowstoneNPS/live",
    "https://www.youtube.com/@VisitNorway/live",         # fjords & northern lights
    "https://www.youtube.com/@NorskNatur/live",          # Norwegian wildlife

    # ── Ocean & marine ──
    "https://www.youtube.com/@OceanExplorationTrust/live",
    "https://www.youtube.com/@NOAAOceanExplorer/live",

    # ── Misc reliable 24/7 nature ──
    "https://www.youtube.com/@EarthCam/live",
    "https://www.youtube.com/@NatureRelaxation/live",
]

RUNTIME = Path("/opt/camstack/runtime")
CACHE_FILE = RUNTIME / "fallback.json"
REDDIT_CACHE_FILE = RUNTIME / "reddit_cams.json"

# ─── Reddit nature-cam discovery ───────────────────────────────────────────

# Subreddits known to host live nature/wildlife stream discussions
_NATURE_SUBREDDITS = [
    "NatureLive",
    "WildlifeWebcams",
    "birding",
    "bears",
    "eagles",
    "wildlife",
    "nature",
    "salmon",
]

# Targeted search queries for viral nature-cam events
_NATURE_SEARCH_QUERIES = [
    "eagle nest cam live",
    "osprey nest webcam live",
    "polar bear live cam",
    "bear feeding webcam",
    "salmon run webcam",
    "bird feeder live cam",
    "whale watching live stream",
    "wolf cam live",
    "nature webcam live stream",
    "penguin cam live",
    "otter cam live",
    "bald eagle nest live",
]

# A post title must match at least one of these to be considered nature content
_NATURE_TITLE_RE = re.compile(
    r'\b('
    r'eagle|osprey|hawk|falcon|owl|heron|puffin|pelican|albatross|crane|stork|kingfisher|'
    r'bear|polar.?bear|grizzly|black.?bear|wolf|bison|elk|moose|deer|fox|lynx|cougar|otter|'
    r'salmon|trout|steelhead|sturgeon|dolphin|whale|orca|seal|sea.?lion|manatee|sea.?turtle|'
    r'penguin|flamingo|'
    r'nest|nesting|hatch|hatchling|fledg|feeding|migration|spawn|spawning|'
    r'bird.?feeder|birdfeed|bird.?cam|'
    r'wildlife|wild.?animal|nature.?cam|nature.?webcam|wildlife.?cam|live.?cam|webcam|'
    r'national.?park|yellowstone|glacier|safari|preserve|sanctuary|refuge'
    r')',
    re.IGNORECASE,
)

# Titles matching any of these are rejected regardless of nature keywords
_EXCLUDE_TITLE_RE = re.compile(
    r'\b('
    r'theme.?park|amusement|boardwalk|wildwood|casino|'
    r'traffic.?cam|road.?cam|weather.?cam|security.?cam|dashboard.?cam|dashcam|'
    r'sports|concert|festival|parade|'
    r'city.?cam|airport|mall|hotel|resort|bar.?cam|pub'
    r')',
    re.IGNORECASE,
)


def get_reddit_nature_cams(use_cache: bool = True, max_age: int = 7200) -> list[str]:
    """
    Search Reddit for viral nature/wildlife webcam links (YouTube only).

    Filters ensure every returned URL:
    - Comes from a post whose title contains a nature/wildlife keyword
    - Does NOT contain urban/entertainment exclusion terms (theme parks, boardwalks, etc.)
    - Points to a YouTube URL

    Results are ranked by Reddit popularity (upvotes + comments) so the most
    engaged streams — eagle nests, bear cams, salmon runs — float to the top.
    """
    if use_cache and REDDIT_CACHE_FILE.exists():
        try:
            data = json.loads(REDDIT_CACHE_FILE.read_text())
            age = time.time() - int(data.get("timestamp", 0))
            if age < max_age and data.get("urls"):
                return data["urls"]
        except Exception:
            pass

    seen: set[str] = set()
    scored: list[tuple[int, str]] = []  # (score, url)

    def _is_youtube(url: str) -> bool:
        return "youtube.com" in url or "youtu.be" in url

    def _normalise_yt(url: str) -> str:
        """Strip tracking params and normalise to watch?v= form."""
        if "youtu.be/" in url:
            vid = url.split("youtu.be/")[-1].split("?")[0].split("/")[0]
            return f"https://www.youtube.com/watch?v={vid}"
        if "watch?v=" in url:
            vid = url.split("watch?v=")[-1].split("&")[0]
            return f"https://www.youtube.com/watch?v={vid}"
        # channel /live pages and other forms kept as-is
        return url.split("?")[0]

    def _scrape(json_url: str) -> None:
        try:
            headers = {"User-Agent": "CamStack/2.0 nature-cam-finder (open source)"}
            resp = requests.get(json_url, headers=headers, timeout=10)
            if resp.status_code != 200:
                return
            payload = resp.json()
            children: list[dict] = []
            if isinstance(payload, list):
                for part in payload:
                    children += part.get("data", {}).get("children", [])
            else:
                children = payload.get("data", {}).get("children", [])

            for child in children:
                post = child.get("data", {})
                title = post.get("title", "")
                url = post.get("url", "")
                score = int(post.get("score", 0)) + int(post.get("num_comments", 0))

                if not _is_youtube(url):
                    continue
                if not _NATURE_TITLE_RE.search(title):
                    continue
                if _EXCLUDE_TITLE_RE.search(title):
                    continue

                clean = _normalise_yt(url)
                if clean not in seen:
                    seen.add(clean)
                    scored.append((score, clean))
        except Exception as exc:
            logger.debug(f"Reddit scrape failed for {json_url}: {exc}")

    # 1. Browse curated nature subreddits (hot + top-of-week)
    for sub in _NATURE_SUBREDDITS:
        _scrape(f"https://www.reddit.com/r/{sub}/hot.json?limit=50")
        _scrape(f"https://www.reddit.com/r/{sub}/top.json?t=week&limit=25")

    # 2. Run targeted search queries for specific viral nature events
    for query in _NATURE_SEARCH_QUERIES:
        encoded = requests.utils.quote(query)
        _scrape(f"https://www.reddit.com/search.json?q={encoded}&sort=top&t=month&limit=25")

    scored.sort(reverse=True)
    urls = [u for _, u in scored]
    logger.info(f"Reddit nature-cam discovery: {len(urls)} qualifying URLs found")

    try:
        REDDIT_CACHE_FILE.write_text(json.dumps({"timestamp": int(time.time()), "urls": urls}, indent=2))
    except Exception:
        pass

    return urls


def get_featured_fallback_url(use_reddit: bool = True, exclude: set[str] | None = None) -> str:
    """Return a URL from the nature-cam pool.

    When Reddit URLs are available, 70% of the time we pick one of those
    (already filtered to genuine nature cams); otherwise we fall back to the
    curated EXPLORE_LIVE_URLS list.
    """
    blocked = exclude or set()
    curated = [u for u in EXPLORE_LIVE_URLS if u not in blocked]

    if use_reddit:
        reddit_urls = [u for u in get_reddit_nature_cams() if u not in blocked]
        if reddit_urls and random.random() < 0.7:
            return random.choice(reddit_urls)

    return random.choice(curated) if curated else random.choice(EXPLORE_LIVE_URLS)


@dataclass(frozen=True)
class LiveStreamInfo:
    url: str
    title: str | None
    viewers: int


def load_cached_stream(max_age: int = 3600) -> LiveStreamInfo | None:
    try:
        data = json.loads(CACHE_FILE.read_text())
    except Exception:
        return None
    ts = int(data.get("timestamp", 0))
    if ts and time.time() - ts > max_age:
        return None
    url = data.get("url")
    if not url:
        return None
    return LiveStreamInfo(url=url, title=data.get("title"), viewers=int(data.get("viewers", 0)))


def save_cached_stream(info: LiveStreamInfo) -> None:
    try:
        payload = {
            "timestamp": int(time.time()),
            "url": info.url,
            "title": info.title,
            "viewers": info.viewers,
        }
        CACHE_FILE.write_text(json.dumps(payload))
    except Exception:
        pass


def _yt_dlp_json(url: str, flat: bool = False, timeout: int = 8) -> dict | None:
    cmd = ["yt-dlp", "--no-warnings", "-J"]
    if flat:
        cmd.append("--flat-playlist")
    cmd.append(url)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except Exception:
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        return json.loads(proc.stdout)
    except Exception:
        return None

def _expand_candidate_urls(seed_urls: Iterable[str], max_entries: int = 25) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add(url: str) -> None:
        if not url or url in seen:
            return
        seen.add(url)
        candidates.append(url)

    for seed in seed_urls:
        data = _yt_dlp_json(seed, flat=True)
        if not data:
            add(seed)
            continue
        entries = data.get("entries") or []
        if not entries:
            add(seed)
            continue
        for e in entries[:max_entries]:
            url = e.get("url") or e.get("id")
            if not url:
                continue
            if "youtube.com" not in url and "youtu.be" not in url:
                url = f"https://www.youtube.com/watch?v={url}"
            add(url)
    return candidates

def _viewer_count(data: dict) -> int:
    if not data:
        return 0
    return int(
        data.get("concurrent_view_count")
        or data.get("view_count")
        or data.get("live_viewers")
        or 0
    )

def get_best_live_stream(max_candidates: int = 30, exclude: set[str] | None = None, use_reddit: bool = True) -> LiveStreamInfo | None:
    """Find the best live nature stream.

    Reddit-discovered URLs (already ranked by community popularity) are prepended
    so they are tried first; curated EXPLORE_LIVE_URLS act as the safety net.
    """
    seed_urls: list[str] = []
    if use_reddit:
        seed_urls = get_reddit_nature_cams()  # pre-filtered & scored
    seed_urls += list(EXPLORE_LIVE_URLS)
    candidates = _expand_candidate_urls(seed_urls)
    blocked = exclude or set()
    best: LiveStreamInfo | None = None
    for url in candidates[:max_candidates]:
        if url in blocked:
            continue
        data = _yt_dlp_json(url)
        if not data or not data.get("is_live"):
            continue
        viewers = _viewer_count(data)
        info = LiveStreamInfo(url=url, title=data.get("title"), viewers=viewers)
        if best is None or info.viewers > best.viewers:
            best = info
    return best
