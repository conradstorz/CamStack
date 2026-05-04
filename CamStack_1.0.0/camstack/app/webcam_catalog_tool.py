#!/usr/bin/env python3
"""Curate, verify, discover, and report on public live webcam feeds.

This tool maintains a JSON catalog of live camera feeds such as nature cams,
cityscapes, construction cameras, public webcams, and timelapse sources.

It also includes a YouTube discovery workflow using yt-dlp. This is useful for
finding public livestreams from channels such as Explore.org without scraping
Explore.org directly.

It is intentionally conservative: it does not try to bypass access controls,
scan private IP ranges, or discover cameras by probing the internet. It only
works with public URLs, public YouTube metadata, and feeds that you choose to
add to the catalog.

Recommended install with uv:

    uv init webcam-catalog
    cd webcam-catalog
    uv add httpx pydantic loguru rich yt-dlp

Optional but strongly recommended:

    Install ffmpeg and make sure it is available on PATH.

Example usage:

    uv run python webcam_catalog_tool.py init
    uv run python webcam_catalog_tool.py seed

    uv run python webcam_catalog_tool.py discover-youtube-channel \
        --url "https://www.youtube.com/@ExploreLiveNatureCams/streams" \
        --source "Explore.org" \
        --category nature \
        --tags explore nature youtube

    uv run python webcam_catalog_tool.py discover-youtube-search \
        --query "powered by EXPLORE.org live cam" \
        --limit 50 \
        --source "Explore.org" \
        --category nature \
        --tags explore nature youtube

    uv run python webcam_catalog_tool.py list
    uv run python webcam_catalog_tool.py verify
    uv run python webcam_catalog_tool.py report
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx
from loguru import logger
from pydantic import BaseModel, Field, HttpUrl, ValidationError, field_validator
from rich.console import Console
from rich.table import Table

APP_DIR = Path(__file__).resolve().parent
BASE_DIR = Path("/opt/camstack")
RUNTIME_DIR = BASE_DIR / "runtime"
LOG_DIR = BASE_DIR / "logs"
CATALOG_PATH = RUNTIME_DIR / "webcam_catalog.json"
THUMBNAIL_DIR = RUNTIME_DIR / "catalog_thumbnails"
REPORT_DIR = RUNTIME_DIR / "catalog_reports"
DISCOVERY_DIR = RUNTIME_DIR / "catalog_discovery"
LOG_PATH = LOG_DIR / "webcam_catalog.log"

HTTP_TIMEOUT_SECONDS = 20
DEFAULT_THUMBNAIL_SECONDS = 12
DEFAULT_YOUTUBE_SEARCH_LIMIT = 50

console = Console()


class Category(StrEnum):
    """High-level feed categories."""

    NATURE = "nature"
    CITY = "city"
    CONSTRUCTION = "construction"
    TRAFFIC = "traffic"
    INDUSTRIAL = "industrial"
    WEATHER = "weather"
    YOUTUBE = "youtube"
    OTHER = "other"


class FeedStatus(StrEnum):
    """Current known feed status."""

    UNKNOWN = "unknown"
    ONLINE = "online"
    OFFLINE = "offline"
    NEEDS_REVIEW = "needs_review"


class DiscoveryStatus(StrEnum):
    """How confident we are that a discovered item belongs in the catalog."""

    IMPORTED = "imported"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    SKIPPED_FILTER = "skipped_filter"
    NEEDS_REVIEW = "needs_review"
    ERROR = "error"


class WebcamFeed(BaseModel):
    """A single curated webcam feed."""

    id: str
    name: str
    category: Category = Category.OTHER
    subcategory: str | None = None
    source: str | None = None
    country: str | None = None
    region: str | None = None
    city: str | None = None
    page_url: HttpUrl | None = None
    stream_url: HttpUrl | None = None
    thumbnail_url: HttpUrl | None = None
    external_id: str | None = None
    external_source: str | None = None
    tags: list[str] = Field(default_factory=list)
    notes: str | None = None
    status: FeedStatus = FeedStatus.UNKNOWN
    quality_score: int | None = Field(default=None, ge=0, le=100)
    last_verified: datetime | None = None
    last_error: str | None = None
    last_thumbnail: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: Any) -> list[str]:
        """Normalize tags to lowercase slug-like strings."""
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        return sorted({str(item).strip().lower().replace(" ", "-") for item in value if str(item).strip()})


class WebcamCatalog(BaseModel):
    """The full webcam catalog."""

    version: str = "2026.05.01.02"
    feeds: list[WebcamFeed] = Field(default_factory=list)

    def find_feed(self, feed_id: str) -> WebcamFeed | None:
        """Find a feed by id."""
        return next((feed for feed in self.feeds if feed.id == feed_id), None)

    def find_by_page_url(self, page_url: str) -> WebcamFeed | None:
        """Find a feed by page URL."""
        return next((feed for feed in self.feeds if str(feed.page_url or "") == page_url), None)

    def find_by_external_id(self, external_source: str, external_id: str) -> WebcamFeed | None:
        """Find a feed by external source/id pair."""
        return next(
            (
                feed
                for feed in self.feeds
                if feed.external_source == external_source and feed.external_id == external_id
            ),
            None,
        )


class DiscoveredYouTubeItem(BaseModel):
    """A normalized YouTube item discovered by yt-dlp."""

    video_id: str | None = None
    title: str
    webpage_url: str
    channel: str | None = None
    channel_id: str | None = None
    uploader: str | None = None
    live_status: str | None = None
    duration: float | None = None
    thumbnail: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class DiscoveryResult(BaseModel):
    """The result of attempting to import a discovered item."""

    status: DiscoveryStatus
    title: str
    url: str | None = None
    feed_id: str | None = None
    reason: str | None = None


def setup_logging() -> None:
    """Configure Loguru logging."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    logger.add(LOG_PATH, level="DEBUG", rotation="1 MB", retention="30 days")


def ensure_directories() -> None:
    """Create required data directories."""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    THUMBNAIL_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    DISCOVERY_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def utc_now_string() -> str:
    """Return a filesystem-safe UTC timestamp."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def make_feed_id(name: str, page_url: str | None, stream_url: str | None) -> str:
    """Generate a stable-ish short feed id."""
    seed = f"{name}|{page_url or ''}|{stream_url or ''}".encode("utf-8")
    digest = hashlib.sha256(seed).hexdigest()[:10]
    slug = "-".join(name.lower().replace("_", "-").split())[:48].strip("-")
    return f"{slug}-{digest}"


def load_catalog() -> WebcamCatalog:
    """Load catalog from disk, creating an empty one if needed."""
    ensure_directories()
    if not CATALOG_PATH.exists():
        catalog = WebcamCatalog()
        save_catalog(catalog)
        return catalog

    try:
        return WebcamCatalog.model_validate_json(CATALOG_PATH.read_text(encoding="utf-8"))
    except ValidationError as exc:
        logger.error(f"Catalog validation failed: {exc}")
        raise


def save_catalog(catalog: WebcamCatalog) -> None:
    """Save catalog to disk."""
    ensure_directories()
    CATALOG_PATH.write_text(catalog.model_dump_json(indent=2), encoding="utf-8")


def http_check(url: str) -> tuple[bool, str | None]:
    """Check whether a web URL responds successfully enough to be useful."""
    try:
        with httpx.Client(
            timeout=HTTP_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": "webcam-catalog-verifier/1.0"},
        ) as client:
            response = client.get(url)
            if 200 <= response.status_code < 400:
                return True, None
            return False, f"HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        return False, str(exc)


def executable_available(name: str) -> bool:
    """Return True if an executable is available on PATH."""
    return shutil.which(name) is not None


def capture_thumbnail(feed: WebcamFeed) -> tuple[bool, str | None, str | None]:
    """Attempt to capture a thumbnail from a stream URL or page URL.

    Returns:
        Tuple of success flag, relative thumbnail path, and error message.
    """
    if not executable_available("ffmpeg"):
        return False, None, "ffmpeg is not available on PATH"

    url = str(feed.stream_url or feed.page_url or "")
    if not url:
        return False, None, "no URL available for thumbnail capture"

    output_name = f"{feed.id}_{utc_now_string()}.jpg"
    output_path = THUMBNAIL_DIR / output_name

    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        str(DEFAULT_THUMBNAIL_SECONDS),
        "-i",
        url,
        "-frames:v",
        "1",
        str(output_path),
    ]

    logger.debug(f"Running thumbnail command: {' '.join(command)}")

    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=45, check=False)
    except subprocess.TimeoutExpired:
        return False, None, "ffmpeg timed out"

    if result.returncode != 0:
        error = result.stderr.strip() or "ffmpeg failed"
        return False, None, error

    relative_path = str(output_path.relative_to(BASE_DIR))
    return True, relative_path, None


def calculate_quality_score(feed: WebcamFeed, page_ok: bool, stream_ok: bool, thumbnail_ok: bool) -> int:
    """Calculate a simple quality score.

    This is intentionally simple and explainable. You can tune the weights later.
    """
    score = 0

    if page_ok:
        score += 25
    if stream_ok:
        score += 35
    if thumbnail_ok:
        score += 25
    if feed.notes:
        score += 5
    if feed.tags:
        score += min(10, len(feed.tags) * 2)

    return min(score, 100)


def verify_feed(feed: WebcamFeed, capture: bool = False) -> WebcamFeed:
    """Verify a single webcam feed."""
    logger.info(f"Verifying {feed.name}")

    page_ok = False
    stream_ok = False
    thumbnail_ok = False
    errors: list[str] = []

    if feed.page_url:
        page_ok, page_error = http_check(str(feed.page_url))
        if page_error:
            errors.append(f"page_url: {page_error}")

    if feed.stream_url:
        # For direct stream URLs, an HTTP check may not prove playability, but it is still useful.
        stream_ok, stream_error = http_check(str(feed.stream_url))
        if stream_error:
            errors.append(f"stream_url: {stream_error}")

    if capture:
        thumbnail_ok, thumbnail_path, thumbnail_error = capture_thumbnail(feed)
        if thumbnail_ok:
            feed.last_thumbnail = thumbnail_path
        elif thumbnail_error:
            errors.append(f"thumbnail: {thumbnail_error}")

    if page_ok or stream_ok or thumbnail_ok:
        feed.status = FeedStatus.ONLINE
        feed.last_error = None
    else:
        feed.status = FeedStatus.OFFLINE if errors else FeedStatus.NEEDS_REVIEW
        feed.last_error = "; ".join(errors) if errors else "No URL could be verified"

    feed.quality_score = calculate_quality_score(feed, page_ok, stream_ok, thumbnail_ok)
    feed.last_verified = datetime.now(UTC)
    feed.updated_at = datetime.now(UTC)

    return feed


def run_yt_dlp_json_lines(target: str, flat_playlist: bool = True) -> list[dict[str, Any]]:
    """Run yt-dlp and return parsed JSON lines.

    yt-dlp can output one JSON object per line when extracting playlists,
    channels, or searches. This helper keeps the subprocess boundary simple
    and avoids importing yt-dlp internals.
    """
    if not executable_available("yt-dlp"):
        raise RuntimeError("yt-dlp is not available. Install with: uv add yt-dlp")

    command = ["yt-dlp", "--dump-json", "--ignore-errors", "--no-warnings"]

    if flat_playlist:
        command.append("--flat-playlist")

    command.append(target)

    logger.debug(f"Running yt-dlp command: {' '.join(command)}")
    result = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)

    if result.returncode != 0 and not result.stdout.strip():
        raise RuntimeError(result.stderr.strip() or "yt-dlp failed without output")

    items: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning(f"Skipping invalid yt-dlp JSON line: {line[:120]}")

    return items


def normalize_youtube_item(raw: dict[str, Any]) -> DiscoveredYouTubeItem | None:
    """Normalize a yt-dlp item into the project discovery model."""
    title = raw.get("title") or raw.get("fulltitle")
    video_id = raw.get("id")

    webpage_url = raw.get("webpage_url") or raw.get("url")
    if webpage_url and not str(webpage_url).startswith("http") and video_id:
        webpage_url = f"https://www.youtube.com/watch?v={video_id}"

    if not title or not webpage_url:
        return None

    raw_tags = raw.get("tags") or []
    if not isinstance(raw_tags, list):
        raw_tags = []

    return DiscoveredYouTubeItem(
        video_id=video_id,
        title=title,
        webpage_url=str(webpage_url),
        channel=raw.get("channel"),
        channel_id=raw.get("channel_id"),
        uploader=raw.get("uploader"),
        live_status=raw.get("live_status") or raw.get("is_live"),
        duration=raw.get("duration"),
        thumbnail=raw.get("thumbnail"),
        description=raw.get("description"),
        tags=[str(tag) for tag in raw_tags],
        raw=raw,
    )


def looks_like_live_cam(item: DiscoveredYouTubeItem, include_archives: bool = False) -> bool:
    """Decide whether a YouTube item looks like a webcam/livestream candidate."""
    haystack = " ".join(
        [
            item.title or "",
            item.description or "",
            " ".join(item.tags),
            item.live_status or "",
        ]
    ).lower()

    livestream_terms = [
        "live",
        "livestream",
        "live stream",
        "webcam",
        "cam",
        "camera",
        "nature cam",
        "wildlife cam",
        "explore.org",
        "powered by explore",
        "time lapse",
        "timelapse",
    ]

    archive_terms = ["highlights", "recorded", "recap", "best of", "compilation"]

    if not include_archives and any(term in haystack for term in archive_terms):
        return False

    return any(term in haystack for term in livestream_terms)


def discovery_item_to_feed(
    item: DiscoveredYouTubeItem,
    category: Category,
    source: str | None,
    extra_tags: list[str],
    notes: str | None,
) -> WebcamFeed:
    """Convert a normalized YouTube discovery item into a WebcamFeed."""
    title_tags = infer_tags_from_title(item.title)
    all_tags = ["youtube", *extra_tags, *title_tags]

    if item.channel:
        all_tags.append(item.channel)

    feed_id = make_feed_id(item.title, item.webpage_url, None)

    return WebcamFeed(
        id=feed_id,
        name=item.title,
        category=category,
        subcategory="youtube-live",
        source=source or item.channel or item.uploader or "YouTube",
        page_url=item.webpage_url,
        thumbnail_url=item.thumbnail,
        external_id=item.video_id,
        external_source="youtube",
        tags=all_tags,
        notes=notes,
    )


def infer_tags_from_title(title: str) -> list[str]:
    """Infer a few useful tags from a video title."""
    lowered = title.lower()
    tags: list[str] = []

    keyword_map = {
        "bear": "bears",
        "bison": "bison",
        "eagle": "eagles",
        "bird": "birds",
        "owl": "owls",
        "panda": "pandas",
        "orca": "orcas",
        "shark": "sharks",
        "reef": "reef",
        "ocean": "ocean",
        "underwater": "underwater",
        "africa": "africa",
        "safari": "safari",
        "northern lights": "northern-lights",
        "aurora": "aurora",
        "city": "city",
        "skyline": "skyline",
        "construction": "construction",
        "timelapse": "timelapse",
        "time lapse": "timelapse",
    }

    for keyword, tag in keyword_map.items():
        if keyword in lowered:
            tags.append(tag)

    return tags


def import_discovered_items(
    catalog: WebcamCatalog,
    items: list[DiscoveredYouTubeItem],
    category: Category,
    source: str | None,
    extra_tags: list[str],
    notes: str | None,
    include_archives: bool,
    dry_run: bool,
) -> list[DiscoveryResult]:
    """Import discovered YouTube items into the catalog."""
    results: list[DiscoveryResult] = []

    for item in items:
        if not looks_like_live_cam(item, include_archives=include_archives):
            results.append(
                DiscoveryResult(
                    status=DiscoveryStatus.SKIPPED_FILTER,
                    title=item.title,
                    url=item.webpage_url,
                    reason="does not look like a live camera feed",
                )
            )
            continue

        if item.video_id and catalog.find_by_external_id("youtube", item.video_id):
            results.append(
                DiscoveryResult(
                    status=DiscoveryStatus.SKIPPED_DUPLICATE,
                    title=item.title,
                    url=item.webpage_url,
                    reason="YouTube video id already exists",
                )
            )
            continue

        if catalog.find_by_page_url(item.webpage_url):
            results.append(
                DiscoveryResult(
                    status=DiscoveryStatus.SKIPPED_DUPLICATE,
                    title=item.title,
                    url=item.webpage_url,
                    reason="page URL already exists",
                )
            )
            continue

        feed = discovery_item_to_feed(
            item=item,
            category=category,
            source=source,
            extra_tags=extra_tags,
            notes=notes,
        )

        if not dry_run:
            catalog.feeds.append(feed)

        results.append(
            DiscoveryResult(
                status=DiscoveryStatus.IMPORTED if not dry_run else DiscoveryStatus.NEEDS_REVIEW,
                title=item.title,
                url=item.webpage_url,
                feed_id=feed.id,
                reason="imported" if not dry_run else "dry run",
            )
        )

    return results


def write_discovery_audit(results: list[DiscoveryResult], source_label: str) -> Path:
    """Write a JSON audit file for a discovery run."""
    safe_label = "-".join(source_label.lower().replace("/", " ").split())[:40]
    path = DISCOVERY_DIR / f"discovery_{safe_label}_{utc_now_string()}.json"
    path.write_text(
        json.dumps([result.model_dump(mode="json") for result in results], indent=2),
        encoding="utf-8",
    )
    return path


def print_discovery_results(results: list[DiscoveryResult]) -> None:
    """Print a summary table for discovery results."""
    table = Table(title="Discovery Results")
    table.add_column("Status")
    table.add_column("Title")
    table.add_column("Reason")

    for result in results:
        table.add_row(result.status, result.title[:90], result.reason or "")

    console.print(table)

    imported = sum(1 for result in results if result.status == DiscoveryStatus.IMPORTED)
    duplicates = sum(1 for result in results if result.status == DiscoveryStatus.SKIPPED_DUPLICATE)
    filtered = sum(1 for result in results if result.status == DiscoveryStatus.SKIPPED_FILTER)
    review = sum(1 for result in results if result.status == DiscoveryStatus.NEEDS_REVIEW)

    console.print(
        f"Imported: {imported} | Duplicates: {duplicates} | "
        f"Filtered: {filtered} | Needs review: {review}"
    )


def command_init(_: argparse.Namespace) -> None:
    """Initialize the catalog file."""
    ensure_directories()
    if CATALOG_PATH.exists():
        console.print(f"Catalog already exists: {CATALOG_PATH}")
        return
    save_catalog(WebcamCatalog())
    console.print(f"Created catalog: {CATALOG_PATH}")


def command_add(args: argparse.Namespace) -> None:
    """Add a feed to the catalog."""
    catalog = load_catalog()
    feed_id = make_feed_id(args.name, args.page_url, args.stream_url)

    if catalog.find_feed(feed_id):
        console.print(f"Feed already exists: {feed_id}")
        return

    feed = WebcamFeed(
        id=feed_id,
        name=args.name,
        category=args.category,
        subcategory=args.subcategory,
        source=args.source,
        country=args.country,
        region=args.region,
        city=args.city,
        page_url=args.page_url,
        stream_url=args.stream_url,
        thumbnail_url=args.thumbnail_url,
        tags=args.tags or [],
        notes=args.notes,
    )
    catalog.feeds.append(feed)
    save_catalog(catalog)
    console.print(f"Added feed: {feed.name} [{feed.id}]")


def command_list(args: argparse.Namespace) -> None:
    """List catalog feeds."""
    catalog = load_catalog()

    table = Table(title="Webcam Catalog")
    table.add_column("Status")
    table.add_column("Score", justify="right")
    table.add_column("Name")
    table.add_column("Category")
    table.add_column("Source")
    table.add_column("Tags")

    feeds = catalog.feeds
    if args.category:
        feeds = [feed for feed in feeds if feed.category == args.category]
    if args.status:
        feeds = [feed for feed in feeds if feed.status == args.status]

    for feed in sorted(feeds, key=lambda item: (item.category, item.name.lower())):
        table.add_row(
            feed.status,
            str(feed.quality_score or ""),
            feed.name[:70],
            feed.category,
            feed.source or "",
            ", ".join(feed.tags[:8]),
        )

    console.print(table)


def command_verify(args: argparse.Namespace) -> None:
    """Verify all feeds or one selected feed."""
    catalog = load_catalog()
    changed = False

    for index, feed in enumerate(catalog.feeds):
        if args.feed_id and feed.id != args.feed_id:
            continue
        catalog.feeds[index] = verify_feed(feed, capture=args.capture_thumbnail)
        changed = True

    if not changed:
        console.print("No matching feed found.")
        return

    save_catalog(catalog)
    console.print("Verification complete.")


def command_report(_: argparse.Namespace) -> None:
    """Generate a Markdown report."""
    catalog = load_catalog()
    report_path = REPORT_DIR / f"webcam_report_{utc_now_string()}.md"

    lines = [
        "# Curated Webcam Feed Report",
        "",
        f"Generated: {datetime.now(UTC).isoformat()}",
        "",
        "| Status | Score | Name | Category | Source | Page | Tags |",
        "|---|---:|---|---|---|---|---|",
    ]

    for feed in sorted(catalog.feeds, key=lambda item: (item.category, item.name.lower())):
        page = str(feed.page_url or feed.stream_url or "")
        link = f"[open]({page})" if page else ""
        lines.append(
            "| "
            f"{feed.status} | "
            f"{feed.quality_score or ''} | "
            f"{feed.name} | "
            f"{feed.category} | "
            f"{feed.source or ''} | "
            f"{link} | "
            f"{', '.join(feed.tags)} |"
        )

    report_path.write_text("\n".join(lines), encoding="utf-8")
    console.print(f"Report written: {report_path}")


def command_seed(_: argparse.Namespace) -> None:
    """Seed the catalog with a few safe, high-quality public webcam pages."""
    catalog = load_catalog()

    seed_feeds = [
        {
            "name": "Brooks Falls Bear Cam",
            "category": Category.NATURE,
            "subcategory": "wildlife",
            "source": "Explore.org",
            "country": "USA",
            "region": "Alaska",
            "page_url": "https://explore.org/livecams/brown-bears/brown-bear-salmon-cam-brooks-falls",
            "tags": ["bears", "river", "salmon", "alaska", "wildlife"],
            "notes": "Best during salmon season.",
        },
        {
            "name": "Northern Lights Cam",
            "category": Category.NATURE,
            "subcategory": "sky",
            "source": "Explore.org",
            "country": "Canada",
            "region": "Manitoba",
            "page_url": "https://explore.org/livecams/zen-den/northern-lights-cam",
            "tags": ["northern-lights", "aurora", "night", "sky"],
        },
        {
            "name": "Times Square Cam",
            "category": Category.CITY,
            "subcategory": "street",
            "source": "EarthCam",
            "country": "USA",
            "region": "New York",
            "city": "New York",
            "page_url": "https://www.earthcam.com/usa/newyork/timessquare/",
            "tags": ["city", "street", "landmark", "night"],
        },
        {
            "name": "New York City Skyline Cam",
            "category": Category.CITY,
            "subcategory": "skyline",
            "source": "EarthCam",
            "country": "USA",
            "region": "New York",
            "city": "New York",
            "page_url": "https://www.earthcam.com/usa/newyork/midtown/skyline/",
            "tags": ["city", "skyline", "landmark"],
        },
        {
            "name": "SkylineWebcams City Cams Directory",
            "category": Category.CITY,
            "subcategory": "directory",
            "source": "SkylineWebcams",
            "page_url": "https://www.skylinewebcams.com/en/live-cams-category/city-cams.html",
            "tags": ["directory", "city", "travel"],
        },
    ]

    added = 0
    for item in seed_feeds:
        feed_id = make_feed_id(item["name"], item.get("page_url"), item.get("stream_url"))
        if catalog.find_feed(feed_id):
            continue
        catalog.feeds.append(WebcamFeed(id=feed_id, **item))
        added += 1

    save_catalog(catalog)
    console.print(f"Seed complete. Added {added} feed(s).")


def command_discover_youtube_channel(args: argparse.Namespace) -> None:
    """Discover YouTube livestream candidates from a channel, streams page, or playlist."""
    catalog = load_catalog()
    raw_items = run_yt_dlp_json_lines(args.url, flat_playlist=not args.full_metadata)
    items = [item for raw in raw_items if (item := normalize_youtube_item(raw))]

    results = import_discovered_items(
        catalog=catalog,
        items=items,
        category=args.category,
        source=args.source,
        extra_tags=args.tags or [],
        notes=args.notes,
        include_archives=args.include_archives,
        dry_run=args.dry_run,
    )

    if not args.dry_run:
        save_catalog(catalog)

    audit_path = write_discovery_audit(results, source_label=args.source or "youtube-channel")
    print_discovery_results(results)
    console.print(f"Discovery audit written: {audit_path}")


def command_discover_youtube_search(args: argparse.Namespace) -> None:
    """Discover YouTube livestream candidates from a yt-dlp search query."""
    catalog = load_catalog()
    limit = args.limit or DEFAULT_YOUTUBE_SEARCH_LIMIT
    target = f"ytsearch{limit}:{args.query}"

    raw_items = run_yt_dlp_json_lines(target, flat_playlist=not args.full_metadata)
    items = [item for raw in raw_items if (item := normalize_youtube_item(raw))]

    results = import_discovered_items(
        catalog=catalog,
        items=items,
        category=args.category,
        source=args.source,
        extra_tags=args.tags or [],
        notes=args.notes,
        include_archives=args.include_archives,
        dry_run=args.dry_run,
    )

    if not args.dry_run:
        save_catalog(catalog)

    audit_path = write_discovery_audit(results, source_label=args.source or "youtube-search")
    print_discovery_results(results)
    console.print(f"Discovery audit written: {audit_path}")


def command_refresh_youtube_details(args: argparse.Namespace) -> None:
    """Refresh YouTube metadata for existing YouTube feeds.

    This command does not download videos. It asks yt-dlp for metadata and updates
    the catalog with better title, thumbnail, and status hints where available.
    """
    catalog = load_catalog()
    updated = 0

    for feed in catalog.feeds:
        if feed.external_source != "youtube" and "youtube" not in feed.tags:
            continue
        if args.feed_id and feed.id != args.feed_id:
            continue
        if not feed.page_url:
            continue

        try:
            raw_items = run_yt_dlp_json_lines(str(feed.page_url), flat_playlist=False)
        except RuntimeError as exc:
            feed.last_error = str(exc)
            feed.updated_at = datetime.now(UTC)
            continue

        if not raw_items:
            feed.last_error = "yt-dlp returned no metadata"
            feed.updated_at = datetime.now(UTC)
            continue

        item = normalize_youtube_item(raw_items[0])
        if not item:
            feed.last_error = "could not normalize yt-dlp metadata"
            feed.updated_at = datetime.now(UTC)
            continue

        feed.name = item.title
        feed.thumbnail_url = item.thumbnail
        feed.external_id = item.video_id or feed.external_id
        feed.external_source = "youtube"
        feed.status = FeedStatus.ONLINE if item.live_status in {"is_live", "live", "True", True} else feed.status
        feed.last_error = None
        feed.updated_at = datetime.now(UTC)
        updated += 1

    save_catalog(catalog)
    console.print(f"Refreshed metadata for {updated} YouTube feed(s).")


def add_common_discovery_args(parser: argparse.ArgumentParser) -> None:
    """Add shared discovery options to a subparser."""
    parser.add_argument("--source", default="Explore.org", help="Human-friendly source label.")
    parser.add_argument("--category", choices=[item.value for item in Category], default=Category.NATURE)
    parser.add_argument("--tags", nargs="*", default=["youtube", "livecam"])
    parser.add_argument("--notes")
    parser.add_argument(
        "--include-archives",
        action="store_true",
        help="Include items that look like archived highlights or recap videos.",
    )
    parser.add_argument(
        "--full-metadata",
        action="store_true",
        help="Ask yt-dlp for full metadata. Slower but richer than flat playlist mode.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be imported without modifying the catalog.",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(description="Curate, discover, and verify live webcam feeds.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Initialize an empty catalog.")
    init_parser.set_defaults(func=command_init)

    seed_parser = subparsers.add_parser("seed", help="Add a small starter set of public webcam pages.")
    seed_parser.set_defaults(func=command_seed)

    add_parser = subparsers.add_parser("add", help="Add a webcam feed.")
    add_parser.add_argument("--name", required=True)
    add_parser.add_argument("--category", choices=[item.value for item in Category], default=Category.OTHER)
    add_parser.add_argument("--subcategory")
    add_parser.add_argument("--source")
    add_parser.add_argument("--country")
    add_parser.add_argument("--region")
    add_parser.add_argument("--city")
    add_parser.add_argument("--page-url")
    add_parser.add_argument("--stream-url")
    add_parser.add_argument("--thumbnail-url")
    add_parser.add_argument("--tags", nargs="*")
    add_parser.add_argument("--notes")
    add_parser.set_defaults(func=command_add)

    list_parser = subparsers.add_parser("list", help="List catalog feeds.")
    list_parser.add_argument("--category", choices=[item.value for item in Category])
    list_parser.add_argument("--status", choices=[item.value for item in FeedStatus])
    list_parser.set_defaults(func=command_list)

    verify_parser = subparsers.add_parser("verify", help="Verify feeds.")
    verify_parser.add_argument("--feed-id", help="Verify only one feed id.")
    verify_parser.add_argument(
        "--capture-thumbnail",
        action="store_true",
        help="Try to capture a thumbnail using ffmpeg.",
    )
    verify_parser.set_defaults(func=command_verify)

    report_parser = subparsers.add_parser("report", help="Generate a Markdown report.")
    report_parser.set_defaults(func=command_report)

    channel_parser = subparsers.add_parser(
        "discover-youtube-channel",
        help="Discover candidate live cams from a YouTube channel, streams page, or playlist.",
    )
    channel_parser.add_argument("--url", required=True, help="YouTube channel, streams, videos, live, or playlist URL.")
    add_common_discovery_args(channel_parser)
    channel_parser.set_defaults(func=command_discover_youtube_channel)

    search_parser = subparsers.add_parser(
        "discover-youtube-search",
        help="Discover candidate live cams from a YouTube search query using yt-dlp.",
    )
    search_parser.add_argument("--query", required=True)
    search_parser.add_argument("--limit", type=int, default=DEFAULT_YOUTUBE_SEARCH_LIMIT)
    add_common_discovery_args(search_parser)
    search_parser.set_defaults(func=command_discover_youtube_search)

    refresh_parser = subparsers.add_parser(
        "refresh-youtube-details",
        help="Refresh metadata for existing YouTube feeds.",
    )
    refresh_parser.add_argument("--feed-id", help="Refresh only one feed id.")
    refresh_parser.set_defaults(func=command_refresh_youtube_details)

    return parser


def main() -> None:
    """CLI entrypoint."""
    setup_logging()
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
