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

@app.get("/api/discover")
def api_discover():
    cams = onvif_discover()
    payload = []
    for c in cams:
        payload.append({
            "ip": c.ip,
            "model": c.model or "Unknown",
            "rtsp_url": c.rtsp_url,
            "snapshot": f"/snaps/{Path(c.snapshot_path).name}" if c.snapshot_path else None,
        })
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
