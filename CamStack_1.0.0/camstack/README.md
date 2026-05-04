# CamStack 2.0.1

A self-hosted Raspberry Pi camera system with a web admin UI, fullscreen mpv player, motion detection, and an automated webcam discovery and recommendation engine.

![UI Preview](docs/ui-preview.png)

---

## Features

- **HTTPS admin UI** — FastAPI + Jinja2 on port 443 (self-signed, private CA, or CSR-only)
- **HTTP → HTTPS redirect** on port 80
- **ONVIF/WS-Discovery** — scans local network for IP cameras
- **Fullscreen mpv player** — plays RTSP streams with an ASS overlay
- **Nature webcam curator** — discovers, scores, and recommends public livestreams from YouTube and other sources; weighted recommendation engine with automatic retirement of dead feeds
- **Webcam catalog tool** — CLI for curating a local feed catalog, discovering new streams via yt-dlp, verifying URLs, and syncing entries into the curator
- **Motion detection** — per-camera motion detection with snapshot and clip recording
- **Reddit discovery** — finds popular nature webcam URLs from Reddit discussions

---

## Requirements

- Raspberry Pi (or any Linux box) with a desktop environment
- Python 3.10+
- `mpv`, `ffmpeg`, `yt-dlp` on PATH
- `uv` (installed automatically by the install script)

---

## Installation

```bash
git clone <repo> /opt/camstack
cd /opt/camstack
sudo bash scripts/install_camstack.sh
```

The script:
1. Installs system packages (`mpv`, `ffmpeg`, `yt-dlp`, `python3-venv`, etc.)
2. Installs `uv` if not present
3. Creates a venv at `/opt/camstack/.venv` and installs all Python dependencies
4. Generates TLS certificates (see [TLS modes](#tls-modes))
5. Installs and starts three systemd services

### TLS modes

Control with the `CAMSTACK_TLS_MODE` environment variable before running the install script:

| Mode | Description |
|---|---|
| `selfsigned` (default) | Self-signed cert. Browser will warn until trusted. |
| `private_ca_local` | Generates a local root CA and signs a cert. Distribute `ca/rootCA.crt` to clients. |
| `csr_only` | Generates a CSR only. Sign externally, then place the cert at `certs/server.crt`. |

```bash
# Example: private CA
CAMSTACK_TLS_MODE=private_ca_local sudo bash scripts/install_camstack.sh
```

---

## Services

Three systemd services are installed:

| Service | Port | Description |
|---|---|---|
| `camstack.service` | 443 | FastAPI web app (HTTPS) |
| `camredirect.service` | 80 | HTTP → HTTPS redirect |
| `camplayer.service` | — | Fullscreen mpv player on the Pi display |

```bash
# Status
sudo systemctl status camstack camplayer camredirect

# Restart
sudo systemctl restart camstack
```

---

## Configuration

Runtime config lives at `/opt/camstack/runtime/config.json`.

```json
{
  "rtsp_url": "rtsp://192.168.1.100:554/stream1",
  "motion_detection": {
    "enabled": false,
    "snapshot_interval": 1.0,
    "sensitivity": 12.0,
    "frame_threshold": 3,
    "rotation_interval": 20,
    "clip_playback_speed": 2.0,
    "cameras": {}
  },
  "curator": {
    "windy_api_key": "",
    "nps_api_key": "",
    "discovery_interval_hours": 24,
    "min_reliability_threshold": 0.2
  }
}
```

### API keys (optional)

- **`windy_api_key`** — Windy.com webcam API. Currently disabled (Windy embeds are image archives, not playable streams).
- **`nps_api_key`** — US National Park Service API. Free at [developer.nps.gov](https://www.nps.gov/subjects/developer/). Adds NPS webcam entries when they expose YouTube streams.

---

## Web API

The admin UI is at `https://<pi-ip>/`. Key API endpoints:

### Local camera discovery

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/discover` | Scan local network for ONVIF cameras |
| `GET` | `/api/cameras` | List previously discovered cameras |
| `POST` | `/api/set_rtsp` | Set the active RTSP stream `{"rtsp_url": "..."}` |

### Webcam curator

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/curator/feeds` | List all feeds with stats |
| `GET` | `/api/curator/feeds/recommend?n=3` | Get n recommended feeds |
| `GET` | `/api/curator/feeds/{id}` | Get one feed with recent events |
| `POST` | `/api/curator/feeds` | Manually add a feed |
| `PATCH` | `/api/curator/feeds/{id}/block` | Block a feed |
| `PATCH` | `/api/curator/feeds/{id}/unblock` | Unblock a feed |
| `POST` | `/api/curator/events` | Report a playback event (played/failed/skipped/rejected) |
| `POST` | `/api/curator/discover` | Trigger a full background discovery run |
| `POST` | `/api/curator/catalog/import` | Import feeds from the webcam catalog JSON immediately |
| `GET` | `/api/curator/stats` | Curator DB statistics |
| `GET` | `/api/curator/blocklist` | List blocklist words |
| `POST` | `/api/curator/blocklist` | Add a blocklist word |
| `DELETE` | `/api/curator/blocklist/{word}` | Remove a blocklist word |

---

## Webcam Catalog Tool

`app/webcam_catalog_tool.py` is a standalone CLI for maintaining a curated JSON catalog of public webcam feeds. It is intentionally conservative — it only works with public URLs and does not scan private IP ranges or bypass access controls.

### Usage

Run from the project root using the installed venv:

```bash
VENV=/opt/camstack/.venv/bin/python

# Initialise an empty catalog
sudo $VENV -m app.webcam_catalog_tool init

# Add the built-in seed feeds (Brooks Falls, Northern Lights, EarthCam, etc.)
sudo $VENV -m app.webcam_catalog_tool seed

# Add a feed manually
sudo $VENV -m app.webcam_catalog_tool add \
    --name "My Cam" --category nature --page-url "https://example.com/cam"

# Discover livestreams from a YouTube channel
sudo $VENV -m app.webcam_catalog_tool discover-youtube-channel \
    --url "https://www.youtube.com/@ExploreLiveNatureCams/streams" \
    --source "Explore.org" --category nature --tags explore nature youtube

# Discover via YouTube search
sudo $VENV -m app.webcam_catalog_tool discover-youtube-search \
    --query "powered by EXPLORE.org live cam" --limit 50 \
    --source "Explore.org" --category nature

# List the catalog
sudo $VENV -m app.webcam_catalog_tool list

# Verify all feeds (HTTP check)
sudo $VENV -m app.webcam_catalog_tool verify

# Verify and capture thumbnails with ffmpeg
sudo $VENV -m app.webcam_catalog_tool verify --capture-thumbnail

# Refresh YouTube titles and thumbnails for existing entries
sudo $VENV -m app.webcam_catalog_tool refresh-youtube-details

# Generate a Markdown report
sudo $VENV -m app.webcam_catalog_tool report
# → written to /opt/camstack/runtime/catalog_reports/
```

All commands support `--dry-run` on the discovery subcommands to preview without modifying the catalog.

### Syncing the catalog into the curator

After running any discovery or add command, push the new entries into the running curator DB immediately:

```bash
curl -X POST http://localhost:8000/api/curator/catalog/import
```

This is also done automatically as part of every scheduled discovery run (every 24 hours by default).

### Catalog file locations

| Path | Description |
|---|---|
| `/opt/camstack/runtime/webcam_catalog.json` | The catalog |
| `/opt/camstack/runtime/catalog_thumbnails/` | ffmpeg-captured thumbnails |
| `/opt/camstack/runtime/catalog_reports/` | Markdown reports |
| `/opt/camstack/runtime/catalog_discovery/` | Per-run JSON audit files |
| `/opt/camstack/logs/webcam_catalog.log` | Tool log |

---

## Keeping the Feed List Updated

### Automatic (no action needed)
The curator runs a discovery cycle every 24 hours (configurable via `discovery_interval_hours`). It pulls from the built-in seed list, NPS API, and the webcam catalog. Feeds that fall below 20% reliability after 10+ plays are automatically retired.

### Weekly — bulk discovery

```bash
VENV=/opt/camstack/.venv/bin/python

sudo $VENV -m app.webcam_catalog_tool discover-youtube-channel \
    --url "https://www.youtube.com/@ExploreLiveNatureCams/streams" \
    --source "Explore.org" --category nature

curl -X POST http://localhost:8000/api/curator/catalog/import
```

### Monthly — verification and cleanup

```bash
VENV=/opt/camstack/.venv/bin/python

# Mark dead feeds offline in the catalog
sudo $VENV -m app.webcam_catalog_tool verify

# Refresh YouTube metadata
sudo $VENV -m app.webcam_catalog_tool refresh-youtube-details

# Generate a report to review the catalog state
sudo $VENV -m app.webcam_catalog_tool report

# Block a specific feed from recommendations
curl -X PATCH http://localhost:8000/api/curator/feeds/<feed_id>/block
```

---

## File Layout

```
/opt/camstack/
├── app/
│   ├── main.py                  # FastAPI app entrypoint
│   ├── webcam_curator.py        # Curator: discovery, scoring, recommendation
│   ├── webcam_catalog_tool.py   # CLI catalog tool
│   ├── discovery.py             # ONVIF/WS-Discovery
│   ├── player.py                # mpv player + watchdog
│   ├── fallback.py              # YouTube fallback stream resolver
│   ├── motion_detector.py       # Per-camera motion detection
│   ├── motion_memory.py         # Motion event persistence
│   ├── overlay_gen.py           # ASS subtitle overlay generator
│   ├── identify_streams.py      # Stream identification helpers
│   ├── redirect_http.py         # HTTP→HTTPS redirector app
│   └── templates/               # Jinja2 HTML templates
├── certs/                       # TLS cert and key (git-ignored)
├── ca/                          # Root CA files (git-ignored)
├── docs/                        # Documentation and screenshots
├── logs/                        # Log files (git-ignored)
├── runtime/                     # Runtime state (git-ignored)
│   ├── config.json              # Main configuration
│   ├── webcams.db               # Curator SQLite database
│   ├── webcam_catalog.json      # Webcam catalog (CLI tool output)
│   └── ...
├── scripts/
│   ├── install_camstack.sh      # Full install script
│   ├── pki.sh                   # TLS cert generation helpers
│   ├── run_camplayer.sh         # Player launch script
│   └── enable_private_ca.sh     # CA trust helper
├── services/                    # systemd unit files
└── pyproject.toml
```

---

## Logs

| File | Description |
|---|---|
| `logs/camstack.log` | Web app (FastAPI) |
| `logs/camplayer.log` | Player process |
| `logs/nature_feed.log` | Nature feed selection events only |
| `logs/webcam_catalog.log` | Catalog CLI tool |

```bash
sudo journalctl -u camstack -f
sudo journalctl -u camplayer -f
tail -f /opt/camstack/logs/nature_feed.log
```
