from __future__ import annotations
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from loguru import logger
import subprocess, json, asyncio, uuid

from .discovery import onvif_discover
from .overlay_gen import write_overlay, get_first_ipv4
from . import identify_streams

BASE = Path("/opt/camstack")
RUNTIME = BASE / "runtime"
CFG = RUNTIME / "config.json"
SNAPS = RUNTIME / "snaps"
VERSION = "1.0.0"

app = FastAPI(title="CamStack", version=VERSION)
app.mount("/snaps", StaticFiles(directory=str(SNAPS)), name="snaps")
templates = Jinja2Templates(directory=str(BASE / "app" / "templates"))

@app.on_event("startup")
def _startup():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    SNAPS.mkdir(parents=True, exist_ok=True)
    write_overlay(False)
    logger.add(str(BASE / "logs" / "camstack.log"), rotation="10 MB")

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
    cams = onvif_discover()
    payload = []
    for c in cams:
        snap_url = f"/snaps/{Path(c.snapshot_path).name}" if c.snapshot_path else None
        logger.debug(f"Camera {c.ip}: snapshot_path={c.snapshot_path}, snap_url={snap_url}")
        payload.append({
            "ip": c.ip,
            "model": c.model or "Unknown",
            "rtsp_url": c.rtsp_url,
            "snapshot": snap_url,
        })
    logger.info(f"Discover returning {len(payload)} cameras")
    return JSONResponse(payload)

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
            "cameras": {}
        })
    
    try:
        cfg = json.loads(CFG.read_text())
        motion_cfg = cfg.get("motion_detection", {
            "enabled": False,
            "snapshot_interval": 1.0,
            "sensitivity": 12.0,
            "frame_threshold": 3,
            "rotation_interval": 20,
            "cameras": {}
        })
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
        motion_cfg = cfg.get("motion_detection", {
            "enabled": False,
            "snapshot_interval": 1.0,
            "sensitivity": 12.0,
            "frame_threshold": 3,
            "rotation_interval": 20,
            "cameras": {}
        })
        
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
        motion_cfg = cfg.get("motion_detection", {})
        cameras = motion_cfg.get("cameras", {})
        return JSONResponse({"cameras": cameras})
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
            cfg["motion_detection"] = {
                "enabled": False,
                "snapshot_interval": 1.0,
                "sensitivity": 12.0,
                "frame_threshold": 3,
                "rotation_interval": 20,
                "cameras": {}
            }
        
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
    """Sync currently discovered cameras to motion detection config."""
    try:
        # Discover cameras
        cams = onvif_discover()
        
        # Load config
        if CFG.exists():
            cfg = json.loads(CFG.read_text())
        else:
            cfg = {}
        
        # Initialize motion config if needed
        if "motion_detection" not in cfg:
            cfg["motion_detection"] = {
                "enabled": False,
                "snapshot_interval": 1.0,
                "sensitivity": 12.0,
                "frame_threshold": 3,
                "rotation_interval": 20,
                "cameras": {}
            }
        
        # Add discovered cameras (preserve existing enabled status)
        existing_cameras = cfg["motion_detection"]["cameras"]
        added = 0
        
        for cam in cams:
            if cam.rtsp_url:  # Only add cameras with valid RTSP URLs
                if cam.ip not in existing_cameras:
                    existing_cameras[cam.ip] = {
                        "rtsp_url": cam.rtsp_url,
                        "enabled": True  # Enable new cameras by default
                    }
                    added += 1
                else:
                    # Update RTSP URL if camera already exists
                    existing_cameras[cam.ip]["rtsp_url"] = cam.rtsp_url
        
        # Save
        CFG.write_text(json.dumps(cfg, indent=2))
        
        return JSONResponse({
            "ok": True,
            "discovered": len(cams),
            "added": added,
            "total": len(existing_cameras)
        })
    except Exception as e:
        logger.exception("Failed to sync discovered cameras")
        return JSONResponse({"error": str(e)}, status_code=500)
