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

EXPLORE_LIVE_URLS = [
    "https://www.youtube.com/watch?v=V7uiqRCW6I8",
    "https://www.youtube.com/@ExploreLiveNatureCams/live",
    "https://www.youtube.com/playlist?list=PLkAmZAcQ2jdpuIim_aop0pVq1z5vdL164",
]

# Subreddits to search for nature webcam discussions
REDDIT_SOURCES = [
    "r/NatureLive",
    "r/livestreaming",
    "r/nature",
    "r/camping",
    "r/Outdoors",
    "r/wildlife",
]

REDDIT_SEARCH_QUERIES = [
    "nature webcam",
    "wildlife webcam",
    "live nature stream",
    "animal webcam",
    "nature cam live",
]

RUNTIME = Path("/opt/camstack/runtime")
CACHE_FILE = RUNTIME / "fallback.json"
REDDIT_CACHE_FILE = RUNTIME / "reddit_cams.json"

def get_featured_fallback_url(use_reddit: bool = True) -> str:
    """
    Get a random featured fallback URL.
    
    Args:
        use_reddit: If True, includes Reddit-discovered URLs in the selection
    
    Returns:
        A random URL from available sources
    """
    urls = list(EXPLORE_LIVE_URLS)
    if use_reddit:
        reddit_urls = get_reddit_nature_cams()
        if reddit_urls:
            # Prefer Reddit URLs (70% chance) if available
            if random.random() < 0.7:
                return random.choice(reddit_urls)
            urls.extend(reddit_urls)
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

def _extract_youtube_urls(text: str) -> list[str]:
    """Extract YouTube URLs from text using regex."""
    if not text:
        return []
    patterns = [
        r'https?://(?:www\.)?youtube\.com/watch\?v=[\w-]+',
        r'https?://(?:www\.)?youtube\.com/@[\w-]+/live',
        r'https?://(?:www\.)?youtube\.com/channel/[\w-]+/live',
        r'https?://youtu\.be/[\w-]+',
        r'https?://(?:www\.)?youtube\.com/embed/[\w-]+',
    ]
    urls = []
    for pattern in patterns:
        urls.extend(re.findall(pattern, text, re.IGNORECASE))
    return list(set(urls))  # Remove duplicates

@dataclass(frozen=True)
class RedditPost:
    """Represents a Reddit post with popularity metrics."""
    title: str
    url: str
    score: int
    num_comments: int
    selftext: str
    
    @property
    def popularity(self) -> int:
        """Combined popularity score (upvotes + comments)."""
        return self.score + self.num_comments

def _fetch_reddit_posts(subreddit: str, sort: str = "hot", limit: int = 25, time_filter: str = "week") -> list[RedditPost]:
    """Fetch posts from a subreddit using Reddit's public JSON API."""
    try:
        url = f"https://www.reddit.com/{subreddit}/{sort}.json"
        params = {"limit": limit, "t": time_filter} if sort == "top" else {"limit": limit}
        headers = {"User-Agent": "CamStack/1.0"}
        
        response = requests.get(url, params=params, headers=headers, timeout=10)
        if response.status_code != 200:
            return []
        
        data = response.json()
        posts = []
        for child in data.get("data", {}).get("children", []):
            post_data = child.get("data", {})
            posts.append(RedditPost(
                title=post_data.get("title", ""),
                url=post_data.get("url", ""),
                score=post_data.get("score", 0),
                num_comments=post_data.get("num_comments", 0),
                selftext=post_data.get("selftext", ""),
            ))
        return posts
    except Exception:
        return []

def _search_reddit(query: str, limit: int = 25, time_filter: str = "week") -> list[RedditPost]:
    """Search Reddit for posts matching a query."""
    try:
        url = "https://www.reddit.com/search.json"
        params = {"q": query, "limit": limit, "t": time_filter, "sort": "relevance"}
        headers = {"User-Agent": "CamStack/1.0"}
        
        response = requests.get(url, params=params, headers=headers, timeout=10)
        if response.status_code != 200:
            return []
        
        data = response.json()
        posts = []
        for child in data.get("data", {}).get("children", []):
            post_data = child.get("data", {})
            posts.append(RedditPost(
                title=post_data.get("title", ""),
                url=post_data.get("url", ""),
                score=post_data.get("score", 0),
                num_comments=post_data.get("num_comments", 0),
                selftext=post_data.get("selftext", ""),
            ))
        return posts
    except Exception:
        return []

def get_reddit_nature_cams(use_cache: bool = True, max_age: int = 7200) -> list[str]:
    """
    Discover popular nature webcam URLs from Reddit discussions.
    
    Returns a list of YouTube URLs, ranked by Reddit popularity (upvotes + comments).
    Results are cached for 2 hours by default.
    """
    # Check cache first
    if use_cache:
        try:
            cache_data = json.loads(REDDIT_CACHE_FILE.read_text())
            ts = int(cache_data.get("timestamp", 0))
            if ts and time.time() - ts <= max_age:
                return cache_data.get("urls", [])
        except Exception:
            pass
    
    # Collect posts from multiple sources
    all_posts: list[RedditPost] = []
    
    # Search specific subreddits
    for subreddit in REDDIT_SOURCES:
        all_posts.extend(_fetch_reddit_posts(subreddit, sort="hot", limit=10))
        all_posts.extend(_fetch_reddit_posts(subreddit, sort="top", limit=15, time_filter="week"))
    
    # Search by keywords
    for query in REDDIT_SEARCH_QUERIES:
        all_posts.extend(_search_reddit(query, limit=20, time_filter="week"))
    
    # Extract YouTube URLs and rank by popularity
    url_scores: dict[str, int] = {}
    for post in all_posts:
        # Check if the post URL itself is a YouTube link
        if "youtube.com" in post.url or "youtu.be" in post.url:
            url_scores[post.url] = max(url_scores.get(post.url, 0), post.popularity)
        
        # Extract URLs from title and selftext
        text_urls = _extract_youtube_urls(f"{post.title} {post.selftext}")
        for url in text_urls:
            url_scores[url] = max(url_scores.get(url, 0), post.popularity)
    
    # Sort by popularity score
    ranked_urls = sorted(url_scores.items(), key=lambda x: x[1], reverse=True)
    result_urls = [url for url, _ in ranked_urls]
    
    # Cache the results
    try:
        cache_data = {
            "timestamp": int(time.time()),
            "urls": result_urls,
        }
        REDDIT_CACHE_FILE.write_text(json.dumps(cache_data))
    except Exception:
        pass
    
    return result_urls

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
    """
    Find the best live nature stream from available sources.
    
    Args:
        max_candidates: Maximum number of stream URLs to check
        exclude: Set of URLs to skip
        use_reddit: Whether to include Reddit-discovered URLs as sources
    
    Returns:
        LiveStreamInfo for the stream with the most viewers, or None if no live streams found
    """
    seed_urls = list(EXPLORE_LIVE_URLS)
    
    # Optionally add Reddit-discovered URLs
    if use_reddit:
        reddit_urls = get_reddit_nature_cams()
        # Prioritize Reddit URLs by putting them first
        seed_urls = reddit_urls + seed_urls
    
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
