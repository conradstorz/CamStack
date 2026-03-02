# CamStack – Copilot Instructions

## Project Priority
Real-time fullscreen display of whichever local-network IP camera is currently seeing motion. Latency and reliability on a headless Pi appliance take precedence over everything else.

## Architecture

Three systemd services, all living in `CamStack_1.0.0/camstack/`:

| Service | File | Role |
|---------|------|------|
| `camstack.service` | `app/main.py` | FastAPI HTTPS admin UI on :443 (Jinja2 + HTMX) |
| `camplayer.service` | `app/player.py` | Fullscreen display: tkinter still-frame → mpv realtime → YouTube fallback |
| `camredirect.service` | `app/redirect_http.py` | HTTP→HTTPS redirect on :80 |

**Key modules:**
- `app/motion_detector.py` — ffmpeg RTSP snapshot grabs + numpy/PIL frame differencing; runs in background threads
- `app/motion_memory.py` — NVR clip recording via ffmpeg; stores last 3 clips per camera under `runtime/clips/`
- `app/player.py` — `launch_with_motion_detection()` is the main live loop; falls back to `_fallback_loop()` which probes camera recovery every 60 s
- `app/fallback.py` — ranks and selects YouTube nature-cam streams when no cameras are reachable
- `app/overlay_gen.py` — writes `runtime/overlay.ass` (ASS subtitle overlay with IP + version)
- `app/discovery.py` — ONVIF WS-Discovery + zeep for LAN camera scanning

**Runtime state** (all under `runtime/`, committed to git if changed intentionally):
- `config.json` — active RTSP URL + motion detection settings + per-camera config
- `discovered_cameras.json` — persisted ONVIF scan results
- `motion_memory.json` — last clip path/timestamp per camera
- `snaps/` — latest still frames (display + motion thumbnails)
- `clips/` — recorded motion mp4 clips

## Deployment / Symlink Model

`/opt/camstack` **is a symlink** to `CamStack_1.0.0/camstack/` in this repo.  
`/etc/systemd/system/cam*.service` files are symlinks to `services/` in this repo.  
**Editing any source file is immediately live** — just restart the relevant service.

## Build & Apply Changes

```bash
# Python syntax check before restarting
cd /opt/camstack && python3 -c "import py_compile; py_compile.compile('app/player.py', doraise=True)"

# Apply code changes
sudo systemctl restart camplayer.service   # player logic, motion, fallback
sudo systemctl restart camstack.service    # FastAPI routes, templates
sudo systemctl daemon-reload && sudo systemctl restart camplayer.service  # after service file edits

# Add a Python dependency
cd /opt/camstack && uv add <package>

# Tail logs
tail -f /opt/camstack/logs/camstack.log
journalctl -u camplayer.service -f
```

## Code Conventions

- `from __future__ import annotations` at the top of every module
- `from pathlib import Path` — never raw strings for paths
- `BASE = Path("/opt/camstack")` as the root anchor; all paths derived from it
- `loguru` for all logging (`from loguru import logger`); no `print()` in production paths
- Background threads are daemon threads; named `"motmem-rec-{id}"` style for debuggability
- Motion-detection threshold: 3 consecutive failing snapshots before declaring a camera offline (`all_offline_fail_threshold = 3` in `player.py`)
- `CAMERA_RECOVERED = 75` sentinel return code from `_fallback_loop()` signals the watchdog loop to re-enter live mode

## Templates & UI

- Dark theme defined in `app/templates/base.html` — reuse `.card`, `.btn`, `.muted`, `.row`, `.grid` classes
- HTMX for simple server-driven interactions; vanilla `fetch()` for JSON API calls
- Nav lives in `base.html`; add new top-level pages by adding an `<a href=... class="btn small">` there and a corresponding `@app.get("/<page>", response_class=HTMLResponse)` route

## Service / Systemd

- `camplayer.service` must be `WantedBy=graphical.target` (not `multi-user.target`) — the ordering cycle bug was the root cause of post-reboot failures
- After changing `WantedBy`, run `systemctl enable` **and** manually remove the stale symlink from the old `.wants/` directory
- `camplayer.service` runs as user `pi` with `DISPLAY=:0`

## Integration Points

- **ONVIF**: `wsdiscovery` for UDP probe, `onvif-zeep` for device detail fetch; timeouts are common — always `try/except` and log as WARNING
- **ffmpeg**: used for RTSP snapshot grabs (5 s timeout), clip recording (12 s default), and display frame pulls
- **mpv**: spawned via `subprocess` with IPC socket; `player.py` wraps it in `_spawn_player()`
- **yt-dlp**: used inside `fallback.py` to resolve YouTube live stream URLs
- Static files: `/snaps` and `/clips` are mounted as FastAPI `StaticFiles`; clips support HTTP range requests for in-browser video seeking
