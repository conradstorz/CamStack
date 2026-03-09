from __future__ import annotations
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from loguru import logger
import subprocess, json, asyncio, uuid, threading, time
from datetime import datetime, timezone
import cv2

from .discovery import onvif_discover
from .overlay_gen import write_overlay, get_first_ipv4
from .motion_memory import MotionMemory, format_motion_age
from . import identify_streams

BASE = Path("/opt/camstack")
RUNTIME = BASE / "runtime"
CFG = RUNTIME / "config.json"
SNAPS = RUNTIME / "snaps"
CLIPS_DIR = RUNTIME / "clips"
DISCOVERED = RUNTIME / "discovered_cameras.json"
VERSION = "2.0.1"

app = FastAPI(title="CamStack", version=VERSION)
app.mount("/snaps", StaticFiles(directory=str(SNAPS)), name="snaps")
CLIPS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/clips", StaticFiles(directory=str(CLIPS_DIR)), name="clips")
templates = Jinja2Templates(directory=str(BASE / "app" / "templates"))

@app.on_event("startup")
def _startup():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    SNAPS.mkdir(parents=True, exist_ok=True)
    if not DISCOVERED.exists():
        DISCOVERED.write_text(json.dumps({"last_scan": None, "cameras": []}, indent=2))
    write_overlay(False)
    logger.add(str(BASE / "logs" / "camstack.log"), rotation="10 MB")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _default_motion_cfg() -> dict:
    return {
        "enabled": False,
        "snapshot_interval": 1.0,
        "sensitivity": 12.0,
        "frame_threshold": 3,
        "rotation_interval": 20,
        "clip_playback_speed": 2.0,
        "cameras": {},
    }


def _load_discovered_store() -> dict:
    if not DISCOVERED.exists():
        return {"last_scan": None, "cameras": []}
    try:
        data = json.loads(DISCOVERED.read_text())
        if isinstance(data, dict) and isinstance(data.get("cameras", []), list):
            return {"last_scan": data.get("last_scan"), "cameras": data.get("cameras", [])}
    except Exception:
        pass
    return {"last_scan": None, "cameras": []}


def _save_discovered_store(store: dict) -> None:
    DISCOVERED.write_text(json.dumps(store, indent=2))


def _normalize_discovered_entry(entry: dict) -> dict:
    return {
        "ip": str(entry.get("ip", "")).strip(),
        "model": entry.get("model") or "Unknown",
        "rtsp_url": entry.get("rtsp_url"),
        "snapshot": entry.get("snapshot"),
        "first_seen": entry.get("first_seen"),
        "last_seen": entry.get("last_seen"),
    }


def _merge_discovered(scanned: list[dict]) -> dict:
    store = _load_discovered_store()
    now = _now_iso()
    by_ip: dict[str, dict] = {}

    for raw in store.get("cameras", []):
        cam = _normalize_discovered_entry(raw)
        if cam["ip"]:
            by_ip[cam["ip"]] = cam

    for raw in scanned:
        cam = _normalize_discovered_entry(raw)
        ip = cam["ip"]
        if not ip:
            continue
        prev = by_ip.get(ip, {})
        merged = {
            "ip": ip,
            "model": cam["model"] or prev.get("model") or "Unknown",
            "rtsp_url": cam.get("rtsp_url") or prev.get("rtsp_url"),
            "snapshot": cam.get("snapshot") or prev.get("snapshot"),
            "first_seen": prev.get("first_seen") or now,
            "last_seen": now,
        }
        by_ip[ip] = merged

    merged_cameras = sorted(by_ip.values(), key=lambda x: x.get("ip", ""))
    store["cameras"] = merged_cameras
    if scanned:
        store["last_scan"] = now
    _save_discovered_store(store)
    return store


def _sync_motion_from_discovered(cameras: list[dict]) -> dict:
    cfg = json.loads(CFG.read_text()) if CFG.exists() else {}
    motion_cfg = cfg.get("motion_detection", _default_motion_cfg())
    motion_cams = motion_cfg.get("cameras", {})

    added = 0
    updated = 0
    changed = False

    for cam in cameras:
        ip = str(cam.get("ip", "")).strip()
        rtsp_url = cam.get("rtsp_url")
        if not ip or not rtsp_url:
            continue

        if ip not in motion_cams:
            motion_cams[ip] = {"rtsp_url": rtsp_url, "enabled": True}
            added += 1
            changed = True
        elif motion_cams[ip].get("rtsp_url") != rtsp_url:
            motion_cams[ip]["rtsp_url"] = rtsp_url
            updated += 1
            changed = True

    motion_cfg["cameras"] = motion_cams
    cfg["motion_detection"] = motion_cfg
    if changed:
        CFG.write_text(json.dumps(cfg, indent=2))

    return {
        "added": added,
        "updated": updated,
        "total": len(motion_cams),
    }

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    ip = get_first_ipv4()
    current = None
    if CFG.exists():
        try:
            current = json.loads(CFG.read_text()).get("rtsp_url")
        except Exception:
            pass
    return templates.TemplateResponse("index.html", {
        "request": request,
        "ip": ip,
        "current": current,
        "version": VERSION
    })


@app.get("/motion", response_class=HTMLResponse)
def motion_page(request: Request):
    """Motion detection configuration page."""
    return templates.TemplateResponse("motion.html", {
        "request": request,
        "version": VERSION
    })


@app.get("/api/discover")
def api_discover():
    scanned_payload: list[dict] = []
    try:
        cams = onvif_discover()
        for c in cams:
            snap_url = f"/snaps/{Path(c.snapshot_path).name}" if c.snapshot_path else None
            logger.debug(f"Camera {c.ip}: snapshot_path={c.snapshot_path}, snap_url={snap_url}")
            scanned_payload.append({
                "ip": c.ip,
                "model": c.model or "Unknown",
                "rtsp_url": c.rtsp_url,
                "snapshot": snap_url,
            })
    except Exception as e:
        logger.exception(f"Discovery scan failed: {e}")

    store = _merge_discovered(scanned_payload)
    sync = _sync_motion_from_discovered(store.get("cameras", []))

    logger.info(
        "Discover returning {} cameras (scanned={})",
        len(store.get("cameras", [])),
        len(scanned_payload),
    )
    return JSONResponse({
        "cameras": store.get("cameras", []),
        "last_scan": store.get("last_scan"),
        "scanned": len(scanned_payload),
        "motion_sync": sync,
    })


@app.get("/api/cameras")
def api_cameras():
    store = _load_discovered_store()
    return JSONResponse({
        "cameras": store.get("cameras", []),
        "last_scan": store.get("last_scan"),
    })

class SetUrl(BaseModel):
    rtsp_url: str

@app.post("/api/set_rtsp")
def set_rtsp(body: SetUrl):
    CFG.write_text(json.dumps({"rtsp_url": body.rtsp_url}, indent=2))
    subprocess.run(["sudo", "systemctl", "restart", "camplayer.service"], check=False)
    return {"ok": True}

# async Identify jobs
JOBS: dict[str, dict] = {}

def _new_job():
    jid = uuid.uuid4().hex[:12]
    JOBS[jid] = {"status": "running", "progress": 0, "lines": [], "result": None, "ip": None}
    return jid

def _update_job(jid: str, progress: int, msg: str):
    j = JOBS.get(jid)
    if not j: return
    j["progress"] = max(0, min(100, progress))
    if msg:
        j["lines"].append(msg)
        if len(j["lines"]) > 10:
            j["lines"] = j["lines"][-10:]

def _finish_job(jid: str, result: dict | None, error: str | None = None):
    j = JOBS.get(jid)
    if not j: return
    if error:
        j["status"] = "error"
        j["error"] = error
    else:
        j["status"] = "done"
        j["result"] = result
        j["progress"] = 100

class IdentifyStart(BaseModel):
    ip: str
    user: str | None = None
    password: str | None = None

@app.post("/api/identify_start")
async def api_identify_start(req: IdentifyStart):
    ip = req.ip
    user = req.user or None
    password = req.password or None
    jid = _new_job()
    JOBS[jid]["ip"] = ip

    async def run():
        try:
            def cb(step: int, msg: str):
                _update_job(jid, step, msg)
            _update_job(jid, 0, f"Starting identify for {ip}")
            result = await asyncio.to_thread(
                identify_streams.identify_with_progress, ip, user, password, cb
            )
            _finish_job(jid, result)
        except Exception as e:
            logger.exception("identify job failed")
            _finish_job(jid, None, str(e))

    asyncio.create_task(run())
    return {"job_id": jid}

@app.get("/api/job_status/{job_id}")
def api_job_status(job_id: str):
    j = JOBS.get(job_id)
    if not j:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    return {
        "status": j["status"],
        "progress": j["progress"],
        "lines": j["lines"],
        "result": j.get("result"),
        "error": j.get("error"),
        "ip": j.get("ip"),
    }

# direct one-off identify
class IdentifyRequest(BaseModel):
    ip: str

@app.post("/api/identify")
def api_identify(req: IdentifyRequest):
    try:
        result = identify_streams.identify_single(req.ip)
        return JSONResponse(result)
    except Exception as e:
        logger.exception("identify failed")
        return JSONResponse({"error": str(e)}, status_code=500)

class TestCredsRequest(BaseModel):
    ip: str
    user: str
    password: str

@app.post("/api/test_creds")
def api_test_creds(req: TestCredsRequest):
    try:
        result = identify_streams.identify_single(req.ip, req.user, req.password)
        return JSONResponse(result)
    except Exception as e:
        logger.exception("test_creds failed")
        return JSONResponse({"error": str(e)}, status_code=500)


# Motion Detection Configuration Management

@app.get("/api/motion/config")
def get_motion_config():
    """Get current motion detection configuration."""
    if not CFG.exists():
        return JSONResponse({
            "enabled": False,
            "snapshot_interval": 1.0,
            "sensitivity": 12.0,
            "frame_threshold": 3,
            "rotation_interval": 20,
            "clip_playback_speed": 2.0,
            "ambient_nature_feed": True,
            "cameras": {}
        })
    
    try:
        cfg = json.loads(CFG.read_text())
        motion_cfg = cfg.get("motion_detection", _default_motion_cfg())
        return JSONResponse(motion_cfg)
    except Exception as e:
        logger.exception("Failed to load motion config")
        return JSONResponse({"error": str(e)}, status_code=500)


class MotionConfigUpdate(BaseModel):
    enabled: bool | None = None
    snapshot_interval: float | None = None
    sensitivity: float | None = None
    frame_threshold: int | None = None
    rotation_interval: int | None = None
    clip_playback_speed: float | None = None
    ambient_nature_feed: bool | None = None


@app.post("/api/motion/config")
def update_motion_config(req: MotionConfigUpdate):
    """Update motion detection settings."""
    try:
        # Load existing config
        if CFG.exists():
            cfg = json.loads(CFG.read_text())
        else:
            cfg = {}
        
        # Get or create motion config
        motion_cfg = cfg.get("motion_detection", _default_motion_cfg())
        
        # Update provided fields
        if req.enabled is not None:
            motion_cfg["enabled"] = req.enabled
        if req.snapshot_interval is not None:
            motion_cfg["snapshot_interval"] = max(0.5, min(5.0, req.snapshot_interval))
        if req.sensitivity is not None:
            motion_cfg["sensitivity"] = max(1.0, min(30.0, req.sensitivity))
        if req.frame_threshold is not None:
            motion_cfg["frame_threshold"] = max(1, min(10, req.frame_threshold))
        if req.rotation_interval is not None:
            motion_cfg["rotation_interval"] = max(5, min(300, req.rotation_interval))
        if req.clip_playback_speed is not None:
            motion_cfg["clip_playback_speed"] = max(0.25, min(16.0, req.clip_playback_speed))
        if req.ambient_nature_feed is not None:
            motion_cfg["ambient_nature_feed"] = req.ambient_nature_feed
        
        # Save back
        cfg["motion_detection"] = motion_cfg
        CFG.write_text(json.dumps(cfg, indent=2))
        
        # Restart player service to apply changes
        subprocess.run(["sudo", "systemctl", "restart", "camplayer.service"], check=False)
        
        return JSONResponse({"ok": True, "config": motion_cfg})
    except Exception as e:
        logger.exception("Failed to update motion config")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/motion/cameras")
def get_motion_cameras():
    """Get list of cameras configured for motion detection."""
    try:
        cfg = json.loads(CFG.read_text()) if CFG.exists() else {}
        motion_cfg = cfg.get("motion_detection", _default_motion_cfg())
        cameras = motion_cfg.get("cameras", {})

        discovered = _load_discovered_store().get("cameras", [])

        if not cameras and discovered:
            _sync_motion_from_discovered(discovered)
            cfg = json.loads(CFG.read_text()) if CFG.exists() else {}
            motion_cfg = cfg.get("motion_detection", _default_motion_cfg())
            cameras = motion_cfg.get("cameras", {})

        return JSONResponse({"cameras": cameras, "discovered": discovered})
    except Exception as e:
        logger.exception("Failed to get motion cameras")
        return JSONResponse({"error": str(e)}, status_code=500)


class AddMotionCamera(BaseModel):
    camera_id: str
    rtsp_url: str
    enabled: bool = True


@app.post("/api/motion/cameras")
def add_motion_camera(req: AddMotionCamera):
    """Add a camera to motion detection monitoring."""
    try:
        # Load existing config
        if CFG.exists():
            cfg = json.loads(CFG.read_text())
        else:
            cfg = {}
        
        # Get or create motion config
        if "motion_detection" not in cfg:
            cfg["motion_detection"] = _default_motion_cfg()
        
        # Add camera
        cfg["motion_detection"]["cameras"][req.camera_id] = {
            "rtsp_url": req.rtsp_url,
            "enabled": req.enabled
        }
        
        # Save
        CFG.write_text(json.dumps(cfg, indent=2))
        
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.exception("Failed to add motion camera")
        return JSONResponse({"error": str(e)}, status_code=500)


class UpdateMotionCamera(BaseModel):
    enabled: bool | None = None
    rtsp_url: str | None = None


@app.patch("/api/motion/cameras/{camera_id}")
def update_motion_camera(camera_id: str, req: UpdateMotionCamera):
    """Update a specific camera's motion detection settings."""
    try:
        if not CFG.exists():
            return JSONResponse({"error": "No configuration found"}, status_code=404)
        
        cfg = json.loads(CFG.read_text())
        motion_cfg = cfg.get("motion_detection", {})
        cameras = motion_cfg.get("cameras", {})
        
        if camera_id not in cameras:
            return JSONResponse({"error": "Camera not found"}, status_code=404)
        
        # Update fields
        if req.enabled is not None:
            cameras[camera_id]["enabled"] = req.enabled
        if req.rtsp_url is not None:
            cameras[camera_id]["rtsp_url"] = req.rtsp_url
        
        # Save
        CFG.write_text(json.dumps(cfg, indent=2))
        
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.exception("Failed to update motion camera")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.delete("/api/motion/cameras/{camera_id}")
def delete_motion_camera(camera_id: str):
    """Remove a camera from motion detection."""
    try:
        if not CFG.exists():
            return JSONResponse({"error": "No configuration found"}, status_code=404)
        
        cfg = json.loads(CFG.read_text())
        motion_cfg = cfg.get("motion_detection", {})
        cameras = motion_cfg.get("cameras", {})
        
        if camera_id in cameras:
            del cameras[camera_id]
            CFG.write_text(json.dumps(cfg, indent=2))
            return JSONResponse({"ok": True})
        else:
            return JSONResponse({"error": "Camera not found"}, status_code=404)
    except Exception as e:
        logger.exception("Failed to delete motion camera")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/motion/sync_discovered")
def sync_discovered_cameras():
    """Sync persisted discovered cameras to motion detection config."""
    try:
        store = _load_discovered_store()
        discovered = store.get("cameras", [])
        sync = _sync_motion_from_discovered(discovered)

        return JSONResponse({
            "ok": True,
            "discovered": len(discovered),
            "added": sync["added"],
            "updated": sync["updated"],
            "total": sync["total"],
            "last_scan": store.get("last_scan"),
        })
    except Exception as e:
        logger.exception("Failed to sync discovered cameras")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/motion/events")
def get_motion_events():
    """Return all recorded motion clips, newest first."""
    try:
        mm = MotionMemory()
        entries = mm.all_entries()
        events = []
        for cam_id, entry in entries.items():
            clip_path = entry.get("clip_path", "")
            filename = Path(clip_path).name if clip_path else ""
            events.append({
                "camera_id": cam_id,
                "clip_url": f"/clips/{filename}" if filename else None,
                "filename": filename,
                "timestamp": entry.get("timestamp"),
                "score": entry.get("score"),
                "ago": format_motion_age(entry["timestamp"]) if entry.get("timestamp") else None,
            })
        events.sort(key=lambda e: e["timestamp"] or 0, reverse=True)
        return JSONResponse({"events": events, "total": len(events)})
    except Exception as e:
        logger.exception("Failed to get motion events")
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# MJPEG Live Stream Proxy
# ---------------------------------------------------------------------------

class _CamGrabber:
    """
    Background thread that maintains a persistent cv2.VideoCapture for one
    RTSP URL, JPEG-encodes every frame, and exposes the latest bytes.
    One instance is shared across all HTTP clients watching the same camera.
    """

    _RECONNECT_DELAY = 5.0
    _JPEG_QUALITY = 72

    def __init__(self, camera_id: str, rtsp_url: str) -> None:
        self.camera_id = camera_id
        self.rtsp_url = rtsp_url
        self._latest: bytes | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"mjpeg-{camera_id}"
        )
        self._thread.start()

    def _run(self) -> None:
        logger.info(f"[MJPEG] Starting grabber for {self.camera_id}")
        while not self._stop.is_set():
            cap = cv2.VideoCapture(self.rtsp_url)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            if not cap.isOpened():
                logger.warning(f"[MJPEG] Could not open {self.camera_id}, retrying in {self._RECONNECT_DELAY}s")
                self._stop.wait(self._RECONNECT_DELAY)
                continue
            logger.info(f"[MJPEG] Connected to {self.camera_id}")
            while not self._stop.is_set():
                ret, frame = cap.read()
                if not ret:
                    logger.warning(f"[MJPEG] Frame read failed for {self.camera_id}, reconnecting")
                    break
                # Scale down to a web-friendly width while preserving aspect ratio
                h, w = frame.shape[:2]
                if w > 960:
                    frame = cv2.resize(frame, (960, int(h * 960 / w)))
                ok, buf = cv2.imencode(
                    ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self._JPEG_QUALITY]
                )
                if ok:
                    with self._lock:
                        self._latest = buf.tobytes()
            cap.release()
            if not self._stop.is_set():
                self._stop.wait(self._RECONNECT_DELAY)
        logger.info(f"[MJPEG] Grabber stopped for {self.camera_id}")

    def latest(self) -> bytes | None:
        with self._lock:
            return self._latest

    def stop(self) -> None:
        self._stop.set()


class _MjpegStreamManager:
    """Lazily creates one _CamGrabber per camera_id, shared across clients."""

    def __init__(self) -> None:
        self._grabbers: dict[str, _CamGrabber] = {}
        self._lock = threading.Lock()

    def _resolve_rtsp(self, camera_id: str) -> str | None:
        """Look up RTSP URL from motion config or discovered cameras store."""
        # Motion detection config (primary source — always has validated URLs)
        try:
            if CFG.exists():
                cfg = json.loads(CFG.read_text())
                cam_cfg = cfg.get("motion_detection", {}).get("cameras", {}).get(camera_id)
                if cam_cfg:
                    return cam_cfg.get("rtsp_url")
        except Exception:
            pass
        # Discovered cameras store
        try:
            store = _load_discovered_store()
            for cam in store.get("cameras", []):
                if cam.get("ip") == camera_id:
                    return cam.get("rtsp_url")
        except Exception:
            pass
        return None

    def get(self, camera_id: str) -> _CamGrabber | None:
        with self._lock:
            if camera_id not in self._grabbers:
                rtsp_url = self._resolve_rtsp(camera_id)
                if not rtsp_url:
                    return None
                self._grabbers[camera_id] = _CamGrabber(camera_id, rtsp_url)
            return self._grabbers[camera_id]

    def stop_all(self) -> None:
        with self._lock:
            for g in self._grabbers.values():
                g.stop()
            self._grabbers.clear()


_stream_manager = _MjpegStreamManager()


async def _mjpeg_generator(grabber: _CamGrabber):
    """Async generator that yields MJPEG frames for StreamingResponse."""
    boundary = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
    while True:
        frame = grabber.latest()
        if frame:
            yield boundary + frame + b"\r\n"
        await asyncio.sleep(0.04)  # ~25fps cap; actual rate limited by grabber


@app.get("/stream/{camera_id:path}")
async def stream_camera(camera_id: str):
    """
    MJPEG stream for a single camera.  camera_id is the camera's IP address.
    Browsers can embed this directly in an <img src="/stream/192.168.4.49">.
    """
    grabber = _stream_manager.get(camera_id)
    if grabber is None:
        return JSONResponse({"error": f"Unknown camera: {camera_id}"}, status_code=404)
    return StreamingResponse(
        _mjpeg_generator(grabber),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/live", response_class=HTMLResponse)
def live_page(request: Request):
    """Live camera feed grid page."""
    return templates.TemplateResponse("live.html", {
        "request": request,
        "version": VERSION,
    })
