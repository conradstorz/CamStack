from __future__ import annotations
from dataclasses import dataclass
import json
import random
import subprocess
import time
from pathlib import Path
from typing import Iterable

# Curated list of reliable 24/7 nature & wildlife live streams.
# All are YouTube channel /live pages which yt-dlp can resolve to the
# current live broadcast automatically.
EXPLORE_LIVE_URLS = [
    # ── Broad nature / wildlife ──
    "https://www.youtube.com/@BBCEarth/live",
    "https://www.youtube.com/@WildEarth/live",
    "https://www.youtube.com/@NatGeo/live",
    "https://www.youtube.com/@AnimalPlanet/live",
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


def get_reddit_nature_cams(use_cache: bool = True, max_age: int = 7200) -> list[str]:
    """Stub — Reddit discovery removed in favour of the curated EXPLORE_LIVE_URLS list."""
    return []


def get_featured_fallback_url(use_reddit: bool = True, exclude: set[str] | None = None) -> str:
    """Return a random URL from the curated nature-cam pool, skipping any blocked entries."""
    urls = list(EXPLORE_LIVE_URLS)
    if exclude:
        filtered = [u for u in urls if u not in exclude]
        if filtered:
            urls = filtered
    return random.choice(urls)


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
    """Find the best live nature stream from the curated EXPLORE_LIVE_URLS pool."""
    seed_urls = list(EXPLORE_LIVE_URLS)
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
