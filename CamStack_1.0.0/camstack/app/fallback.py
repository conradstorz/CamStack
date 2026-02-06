from __future__ import annotations
from dataclasses import dataclass
import json
import random
import subprocess
import time
from pathlib import Path
from typing import Iterable

EXPLORE_LIVE_URLS = [
    "https://www.youtube.com/watch?v=V7uiqRCW6I8",
    "https://www.youtube.com/@ExploreLiveNatureCams/live",
    "https://www.youtube.com/playlist?list=PLkAmZAcQ2jdpuIim_aop0pVq1z5vdL164",
]

RUNTIME = Path("/opt/camstack/runtime")
CACHE_FILE = RUNTIME / "fallback.json"

def get_featured_fallback_url() -> str:
    return random.choice(EXPLORE_LIVE_URLS)

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

def get_best_live_stream(max_candidates: int = 30, exclude: set[str] | None = None) -> LiveStreamInfo | None:
    candidates = _expand_candidate_urls(EXPLORE_LIVE_URLS)
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
