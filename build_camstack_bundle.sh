#!/usr/bin/env bash
set -euo pipefail

VERSION="1.0.0"
ROOT="CamStack_${VERSION}"
CAMROOT="$ROOT/camstack"

echo "[*] Cleaning old working dir (if any)..."
rm -rf "$ROOT" "${ROOT}.zip"
mkdir -p "$CAMROOT"
mkdir -p "$CAMROOT/app/templates"
mkdir -p "$CAMROOT/runtime/snaps"
mkdir -p "$CAMROOT/runtime"
mkdir -p "$CAMROOT/logs"
mkdir -p "$CAMROOT/services"
mkdir -p "$CAMROOT/scripts"
mkdir -p "$CAMROOT/certs"
mkdir -p "$CAMROOT/ca"
mkdir -p "$CAMROOT/docs"

# ------------------------------------------------------------------------------
# 1. pyproject.toml
# ------------------------------------------------------------------------------
cat > "$CAMROOT/pyproject.toml" <<"EOF"
[project]
name = "camstack"
version = "1.0.0"
requires-python = ">=3.10"
dependencies = [
  "fastapi",
  "uvicorn[standard]",
  "jinja2",
  "python-multipart",
  "loguru",
  "wsdiscovery",
  "onvif-zeep",
  "psutil",
  "requests",
  "starlette",
]

[tool.uv]
index-url = "https://pypi.org/simple"
EOF

# runtime placeholders
echo "{}" > "$CAMROOT/runtime/config.json"
echo "[]" > "$CAMROOT/runtime/identify_report.json"

# placeholder preview image just so docs/ isn't empty
base64 -d > "$CAMROOT/docs/ui-preview.png" <<"EOF"
iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGMAAQAABQABJ9kZAAAAAElFTkSuQmCC
EOF

# ------------------------------------------------------------------------------
# 2. app/overlay_gen.py
# ------------------------------------------------------------------------------
cat > "$CAMROOT/app/overlay_gen.py" <<"EOF"
from __future__ import annotations
from pathlib import Path
from loguru import logger
import psutil, socket

RUNTIME = Path("/opt/camstack/runtime")
OVERLAY = RUNTIME / "overlay.ass"
VERSION = "1.0.0"

def get_first_ipv4() -> str:
    for name, addrs in psutil.net_if_addrs().items():
        for a in addrs:
            if a.family == socket.AF_INET:
                ip = a.address
                if ip and not ip.startswith("127."):
                    return ip
    return "0.0.0.0"

def write_overlay(fallback: bool = False) -> Path:
    ip = get_first_ipv4()
    admin = f"https://{ip}/"
    tag = "(Fallback) " if fallback else ""

    text = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "WrapStyle: 2\n"
        "PlayResX: 1920\n"
        "PlayResY: 1080\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, "
        "StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: HUD,Arial,24,&H00FFFFFF,&H000000FF,&H80000000,&H64000000,0,0,0,0,100,100,0,0,1,2,0,2,30,30,20,0\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        f"Dialogue: 0,0:00:00.00,9:59:59.00,HUD,,0000,0000,0000,,{{{{\\an2}}}}{tag}CamStack v{VERSION} • Device IP: {ip} • {admin}\n"
    )

    OVERLAY.write_text(text, encoding="utf-8")
    logger.info(f"overlay written to {OVERLAY}")
    return OVERLAY

if __name__ == "__main__":
    write_overlay(False)
EOF

# ------------------------------------------------------------------------------
# 3. app/discovery.py
# ------------------------------------------------------------------------------
cat > "$CAMROOT/app/discovery.py" <<"EOF"
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from loguru import logger
from wsdiscovery.discovery import ThreadedWSDiscovery as WSD
from onvif import ONVIFCamera
import subprocess
import requests

SNAP_DIR = Path("/opt/camstack/runtime/snaps")
SNAP_DIR.mkdir(parents=True, exist_ok=True)

@dataclass
class CamInfo:
    ip: str
    model: str | None
    rtsp_url: str | None
    snapshot_path: str | None

def _safe_filename(ip: str) -> str:
    return ip.replace(":", "_").replace(".", "_")

def onvif_discover(timeout: int = 4) -> list[CamInfo]:
    wsd = WSD()
    wsd.start()
    try:
        svcs = wsd.searchServices(timeout=timeout)
    finally:
        wsd.stop()

    cams: list[CamInfo] = []
    for s in svcs:
        xaddrs = getattr(s, "getXAddrs", lambda: [])()
        ip = None
        for xa in xaddrs or []:
            if "://" in xa:
                host = xa.split("://", 1)[1].split("/", 1)[0]
                ip = host.split(":")[0]
                break
        if not ip:
            continue

        model, rtsp_url, snap_path = None, None, None
        try:
            cam = ONVIFCamera(ip, 80, "", "", wsdl=None, encrypt=False)
            dev_mgmt = cam.create_devicemgmt_service()
            try:
                info = dev_mgmt.GetDeviceInformation()
                model = getattr(info, "Model", None)
            except Exception as e:
                logger.debug(f"GetDeviceInformation failed for {ip}: {e}")

            media = cam.create_media_service()
            profiles = media.GetProfiles()
            if profiles:
                prof = profiles[0]
                try:
                    uri = media.GetStreamUri({
                        "StreamSetup": {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}},
                        "ProfileToken": prof.token,
                    })
                    rtsp_url = getattr(uri, "Uri", None)
                except Exception as e:
                    logger.debug(f"GetStreamUri failed for {ip}: {e}")

                try:
                    snap_uri = media.GetSnapshotUri({"ProfileToken": prof.token}).Uri
                    snap_path = str(_download_snapshot(snap_uri, ip))
                except Exception:
                    if rtsp_url:
                        try:
                            snap_path = str(_grab_frame(rtsp_url, ip))
                        except Exception as e:
                            logger.debug(f"Frame grab failed for {ip}: {e}")
        except Exception as e:
            logger.warning(f"ONVIF detail fetch failed for {ip}: {e}")

        cams.append(CamInfo(ip=ip, model=model, rtsp_url=rtsp_url, snapshot_path=snap_path))

    return cams

def _download_snapshot(url: str, ip: str) -> Path:
    fn = SNAP_DIR / f"{_safe_filename(ip)}.jpg"
    r = requests.get(url, timeout=4, verify=False)
    r.raise_for_status()
    fn.write_bytes(r.content)
    return fn

def _grab_frame(rtsp_url: str, ip: str) -> Path:
    fn = SNAP_DIR / f"{_safe_filename(ip)}.jpg"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp", "-i", rtsp_url,
        "-frames:v", "1", "-q:v", "3", "-y", str(fn)
    ]
    subprocess.run(cmd, check=True, timeout=8)
    return fn
EOF

# ------------------------------------------------------------------------------
# 4. app/fallback.py
# ------------------------------------------------------------------------------
cat > "$CAMROOT/app/fallback.py" <<"EOF"
from __future__ import annotations
import random

EXPLORE_LIVE_URLS = [
    "https://www.youtube.com/watch?v=V7uiqRCW6I8",
    "https://www.youtube.com/@ExploreLiveNatureCams/live",
    "https://www.youtube.com/playlist?list=PLkAmZAcQ2jdpuIim_aop0pVq1z5vdL164",
]

def get_featured_fallback_url() -> str:
    return random.choice(EXPLORE_LIVE_URLS)
EOF

# ------------------------------------------------------------------------------
# 5. app/player.py
# ------------------------------------------------------------------------------
cat > "$CAMROOT/app/player.py" <<"EOF"
from __future__ import annotations
from pathlib import Path
import subprocess, json
from loguru import logger
from .overlay_gen import write_overlay
from .fallback import get_featured_fallback_url

BASE = Path("/opt/camstack")
CFG = BASE / "runtime/config.json"
OVL = BASE / "runtime/overlay.ass"

def run_player_once(url: str) -> int:
    write_overlay(False)
    cmd = [
        "mpv", "--hwdec=auto", "--fs", "--force-window=yes", "--osc=no",
        "--no-input-default-bindings", "--sub-file", str(OVL), "--sid=1",
        "--no-border", "--really-quiet", "--msg-level", "all=fatal",
        "--network-timeout=15", "--rtsp-transport=tcp",
        "--demuxer-max-bytes=32MiB", "--cache-secs=10",
        "--demuxer-readahead-secs=5", url
    ]
    logger.info(f"Launching mpv: {url}")
    proc = subprocess.run(cmd)
    return proc.returncode

def launch_rtsp_then_fallback() -> int:
    url = None
    if CFG.exists():
        try:
            url = json.loads(CFG.read_text()).get("rtsp_url")
        except Exception:
            pass
    if url:
        rc = run_player_once(url)
        if rc == 0:
            return rc
    write_overlay(True)
    fb = get_featured_fallback_url()
    logger.warning("RTSP missing or failed; switching to fallback nature cam")
    return run_player_once(fb)

def launch_rtsp_with_watchdog() -> int:
    """Launch player with systemd watchdog support and health monitoring."""
    import os, time, signal, threading
    
    # Check if running under systemd with watchdog
    watchdog_usec = os.environ.get("WATCHDOG_USEC")
    watchdog_enabled = watchdog_usec is not None
    
    if watchdog_enabled:
        watchdog_interval = int(watchdog_usec) / 2_000_000  # Send notification at half interval
        logger.info(f"Systemd watchdog enabled, interval: {watchdog_interval}s")
        
        def notify_watchdog():
            """Periodically notify systemd that we're alive."""
            while True:
                try:
                    # Send watchdog keep-alive to systemd
                    subprocess.run(["systemd-notify", "WATCHDOG=1"], 
                                 check=False, capture_output=True)
                    time.sleep(watchdog_interval)
                except Exception as e:
                    logger.debug(f"Watchdog notification failed: {e}")
                    time.sleep(10)
        
        # Start watchdog thread
        wd_thread = threading.Thread(target=notify_watchdog, daemon=True)
        wd_thread.start()
    
    # Notify systemd we're ready
    subprocess.run(["systemd-notify", "--ready"], check=False, capture_output=True)
    
    # Launch player with watchdog monitoring
    url = None
    if CFG.exists():
        try:
            url = json.loads(CFG.read_text()).get("rtsp_url")
        except Exception:
            pass
    
    if url:
        logger.info(f"Attempting RTSP stream: {url}")
        rc = run_player_once(url)
        if rc == 0:
            return rc
        logger.warning(f"RTSP player exited with code {rc}")
    
    # Fallback to nature cam
    write_overlay(True)
    fb = get_featured_fallback_url()
    logger.warning("RTSP missing or failed; switching to fallback nature cam")
    return run_player_once(fb)
EOF

# ------------------------------------------------------------------------------
# 6. app/identify_streams.py
# ------------------------------------------------------------------------------
cat > "$CAMROOT/app/identify_streams.py" <<"EOF"
#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, subprocess
from pathlib import Path
from typing import Optional
import requests
from loguru import logger

try:
    from onvif import ONVIFCamera
    ONVIF_AVAILABLE = True
except Exception:
    ONVIF_AVAILABLE = False

BASE = Path("/opt/camstack")
SNAPS = BASE / "runtime" / "snaps"
REPORT = BASE / "runtime" / "identify_report.json"
SNAPS.mkdir(parents=True, exist_ok=True)

COMMON_SNAPSHOT_PATHS = [
    "/snapshot.jpg", "/image.jpg", "/image.png", "/jpg/image.jpg",
    "/cgi-bin/snapshot.cgi", "/cgi-bin/CGIStream.cgi?cmd=snap&usr=&pwd=",
    "/cgi-bin/CGIProxy.fcgi?cmd=snapPicture2&usr=&pwd=",
    "/videostream.cgi?user=admin&pwd=", "/image.jpg?size=2",
    "/axis-cgi/jpg/image.cgi", "/axis-cgi/jpg/image.jpg",
    "/ISAPI/Streaming/channels/101/picture", "/ISAPI/Streaming/channels/102/picture",
    "/cgi-bin/snapshot2.cgi", "/cgi-bin/snapshot.cgi",
    "/cgi-bin/api.cgi?cmd=Snap&channel=0",
    "/cgi-bin/snapshot.cgi?channel=1",
    "/entry.cgi?view=surveillance&cmd=snapshot&cameraId=1",
    "/cgi-bin/viewer/snapshot.jpg", "/snapshot.jpeg",
    "/onvif/snapshot", "/onvif/media_service/snapshot",
    "/SnapshotJPEG?Resolution=640x480",
    "/Streaming/Channels/101/picture",
    "/webapi/entry.cgi?api=SYNO.SurveillanceStation.Camera&method=GetSnapshot&version=1&cameraId=1",
]

COMMON_RTSP_TEMPLATES = [
    "rtsp://{ip}/live.sdp",
    "rtsp://{ip}:554/Streaming/Channels/101",
    "rtsp://{ip}:554/Streaming/Channels/102",
    "rtsp://{ip}/Streaming/Channels/101",
    "rtsp://{ip}/h264/ch1/main/av_stream",
    "rtsp://{ip}/ISAPI/Streaming/Channels/101",
    "rtsp://{ip}/cam/realmonitor?channel=1&subtype=0",
    "rtsp://{ip}:554/cam/realmonitor?channel=1&subtype=0",
    "rtsp://{ip}/axis-media/media.amp",
    "rtsp://{ip}/h264Preview_01_main",
    "rtsp://{ip}/videoMain", "rtsp://{ip}/videoSub",
    "rtsp://{ip}:554/stream1", "rtsp://{ip}:554/stream2",
    "rtsp://{ip}/unicast", "rtsp://{ip}/rtsp/1",
    "rtsp://{ip}/LiveMedia/stream1",
    "rtsp://{ip}/mediaportal/stream1",
]

VENDOR_HINTS = {
    "axis": ["Axis", "axis-media"],
    "hikvision": ["Hikvision", "Hikvision-Webs"],
    "dahua": ["Dahua", "Dahua Technology"],
    "reolink": ["Reolink"],
    "amcrest": ["Amcrest"],
    "synology": ["Synology", "SurveillanceStation"],
    "lorex": ["Lorex", "Dahua"],
    "uniview": ["UNV", "Uniview"],
    "panasonic": ["Panasonic"],
    "bosch": ["Bosch"],
}

def is_port_open(ip: str, port: int, timeout: float = 1.0) -> bool:
    import socket as pysock
    try:
        with pysock.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False

def try_http_snapshot(ip: str, path: str, timeout=4, auth: Optional[tuple]=None, use_https=False):
    scheme = "https" if use_https else "http"
    url = f"{scheme}://{ip}{path}"
    try:
        r = requests.get(url, timeout=timeout, auth=auth, verify=False)
        if r.status_code == 200 and r.headers.get("content-type","").startswith("image"):
            fn = SNAPS / f"{ip}_{path.strip('/').replace('/','_')}.jpg"
            fn.write_bytes(r.content)
            return fn
    except Exception as e:
        logger.debug(f"snapshot try failed {url}: {e}")
    return None

def try_ffmpeg_frame(rtsp_url: str, ip: str, timeout: int = 8):
    fn = SNAPS / f"{ip}_ffmpeg_{abs(hash(rtsp_url)) % (10**8)}.jpg"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp", "-i", rtsp_url,
        "-frames:v", "1", "-q:v", "3", "-y", str(fn)
    ]
    try:
        subprocess.run(cmd, check=True, timeout=timeout)
        if fn.exists() and fn.stat().st_size > 0:
            return fn
    except Exception as e:
        logger.debug(f"ffmpeg failed for {rtsp_url}: {e}")
    return None

def try_onvif(ip: str, user: Optional[str], passwd: Optional[str]):
    result = {"onvif_ok": False, "snapshot_uri": None, "stream_uri": None, "error": None}
    if not ONVIF_AVAILABLE:
        result["error"] = "onvif not installed"
        return result
    try:
        cam = ONVIFCamera(ip, 80, user or "", passwd or "", wsdl=None, encrypt=False)
        media = cam.create_media_service()
        profiles = media.GetProfiles()
        if profiles:
            prof = profiles[0]
            try:
                snap = media.GetSnapshotUri({"ProfileToken": prof.token})
                result["snapshot_uri"] = getattr(snap, "Uri", None)
            except Exception as e:
                logger.debug(f"ONVIF snapshot URI failed: {e}")
            try:
                stream = media.GetStreamUri({
                    "StreamSetup":{"Stream":"RTP-Unicast","Transport":{"Protocol":"RTSP"}},
                    "ProfileToken": prof.token})
                result["stream_uri"] = getattr(stream, "Uri", None)
            except Exception as e:
                logger.debug(f"ONVIF stream URI failed: {e}")
            result["onvif_ok"] = True
    except Exception as e:
        result["error"] = str(e)
    return result

def probe_http_headers(ip: str, port:int=80, use_https=False):
    scheme = "https" if use_https else "http"
    url = f"{scheme}://{ip}:{port}/"
    try:
        r = requests.get(url, timeout=3, verify=False)
        headers = {k:v for k,v in r.headers.items()}
        return {"status": r.status_code, "headers": headers, "server": headers.get("Server")}
    except Exception as e:
        return {"error": str(e)}

def identify_with_progress(ip: str, user: Optional[str]=None, passwd: Optional[str]=None,
                           progress=None) -> dict:
    def tick(step: int, msg: str):
        if progress: progress(step, msg)

    result = {
        "ip": ip, "open_ports": [], "http_probe": {},
        "http_snapshots": [], "https_snapshots": [],
        "onvif": None, "rtsp_found": [],
        "likely_vendors": [], "notes": []
    }
    tick(10, "Checking common ports")
    for p in [80,443,554,8000,8080,8443,7001,8554,5000,5001]:
        if is_port_open(ip, p):
            result["open_ports"].append(p)

    tick(20, "Probing HTTP/HTTPS headers")
    for p in [80, 8080, 8000]:
        probe = probe_http_headers(ip, port=p, use_https=False)
        if probe and "error" not in probe:
            result["http_probe"][f"http:{p}"] = probe
            server = probe.get("server") if isinstance(probe, dict) else None
            if server:
                for vendor, tokens in VENDOR_HINTS.items():
                    for t in tokens:
                        if t.lower() in (server or "").lower():
                            result["likely_vendors"].append(vendor)
    for p in [443, 8443, 5001]:
        probe = probe_http_headers(ip, port=p, use_https=True)
        if probe and "error" not in probe:
            result["http_probe"][f"https:{p}"] = probe
            server = probe.get("server") if isinstance(probe, dict) else None
            if server:
                for vendor, tokens in VENDOR_HINTS.items():
                    for t in tokens:
                        if t.lower() in (server or "").lower():
                            result["likely_vendors"].append(vendor)
    result["likely_vendors"] = sorted(set(result["likely_vendors"]))

    tick(35, "Trying unauthenticated snapshots")
    for path in COMMON_SNAPSHOT_PATHS:
        s = try_http_snapshot(ip, path, auth=None, use_https=False)
        if s: result["http_snapshots"].append(str(s))
        s2 = try_http_snapshot(ip, path, auth=None, use_https=True)
        if s2: result["https_snapshots"].append(str(s2))

    tick(50, "ONVIF probe")
    if ONVIF_AVAILABLE:
        onv = try_onvif(ip, None, None)
        if onv.get("onvif_ok"):
            result["onvif"] = onv
            if onv.get("snapshot_uri"):
                try:
                    r = requests.get(onv["snapshot_uri"], timeout=4, verify=False)
                    if r.status_code == 200 and r.headers.get("content-type","").startswith("image"):
                        fn = SNAPS / f"{ip}_onvif.jpg"
                        fn.write_bytes(r.content)
                        result["http_snapshots"].append(str(fn))
                except Exception as e:
                    logger.debug(f"fetch onvif snapshot failed: {e}")
        elif user and passwd:
            onv2 = try_onvif(ip, user, passwd)
            if onv2.get("onvif_ok"):
                result["onvif"] = onv2
                result["notes"].append("ONVIF worked with supplied creds")

    tick(70, "Trying unauthenticated RTSP candidates")
    for t in COMMON_RTSP_TEMPLATES:
        candidate = t.format(ip=ip)
        ff = try_ffmpeg_frame(candidate, ip)
        if ff:
            result["rtsp_found"].append({"url": candidate, "thumbnail": str(ff)})

    if user and passwd:
        tick(85, "Testing credentialed endpoints")
        for p in COMMON_SNAPSHOT_PATHS:
            s = try_http_snapshot(ip, p, auth=(user, passwd), use_https=False)
            if s: result["http_snapshots"].append(str(s))
            s2 = try_http_snapshot(ip, p, auth=(user, passwd), use_https=True)
            if s2: result["https_snapshots"].append(str(s2))

        for t in COMMON_RTSP_TEMPLATES:
            candidate = t.format(ip=ip)
            if "://" in candidate:
                cand_with = candidate.split("://",1)[0] + "://" + f"{user}:{passwd}@" + candidate.split("://",1)[1]
                ff = try_ffmpeg_frame(cand_with, ip)
                if ff:
                    result["rtsp_found"].append({"url": cand_with, "thumbnail": str(ff)})
    tick(95, "Saving report")
    report_array = []
    if REPORT.exists():
        try:
            report_array = json.loads(REPORT.read_text())
        except Exception:
            report_array = []
    report_array = [r for r in report_array if r.get("ip") != ip]
    report_array.append(result)
    REPORT.write_text(json.dumps(report_array, indent=2))
    tick(100, "Done")
    return result

def identify_single(ip: str, user: Optional[str]=None, passwd: Optional[str]=None) -> dict:
    return identify_with_progress(ip, user, passwd, None)

def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ip", help="single IP to test")
    group.add_argument("--ips-file", help="file with one IP per line")
    parser.add_argument("--user", help="optional username", default=None)
    parser.add_argument("--password", help="optional password", default=None)
    args = parser.parse_args()
    ips = [args.ip] if args.ip else [l.strip() for l in open(args.ips_file) if l.strip()]
    report = []
    for ip in ips:
        logger.info(f"Scanning: {ip}")
        r = identify_with_progress(ip, args.user, args.password, lambda s,m: None)
        report.append(r)
    REPORT.write_text(json.dumps(report, indent=2))
    print("Saved thumbnails to:", SNAPS)
    print("Report location:", REPORT)

if __name__ == "__main__":
    main()
EOF
chmod +x "$CAMROOT/app/identify_streams.py"

# ------------------------------------------------------------------------------
# 7. app/redirect_http.py
# ------------------------------------------------------------------------------
cat > "$CAMROOT/app/redirect_http.py" <<"EOF"
from starlette.applications import Starlette
from starlette.responses import RedirectResponse
from starlette.requests import Request
from starlette.routing import Route

async def do_redirect(request: Request):
    host = request.headers.get("host", "").split(":")[0]
    path = request.url.path or "/"
    query = ("?" + request.url.query) if request.url.query else ""
    return RedirectResponse(url=f"https://{host}{path}{query}", status_code=308)

routes = [Route("/{path:path}", do_redirect)]
app = Starlette(routes=routes)
EOF

# ------------------------------------------------------------------------------
# 8. app/main.py
# ------------------------------------------------------------------------------
cat > "$CAMROOT/app/main.py" <<"EOF"
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
EOF

# ------------------------------------------------------------------------------
# 9. app/templates/base.html
# ------------------------------------------------------------------------------
cat > "$CAMROOT/app/templates/base.html" <<"EOF"
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CamStack 1.0.0</title>
  <script src="https://unpkg.com/htmx.org@1.9.12"></script>
  <style>
    body { background:#0f1115; color:#e6e6e6; font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }
    .container { max-width: 1100px; margin: 24px auto; padding: 0 16px; }
    .btn { background:#1f6feb; color:white; border:none; padding:10px 14px; border-radius:10px; cursor:pointer; }
    .btn.small { padding:6px 10px; font-size: 14px; }
    .grid { display:flex; flex-wrap:wrap; gap:12px; }
    .card { background:#161a22; border:1px solid #1f2430; border-radius:12px; padding:14px; }
    .cam-card img { display:block; width:320px; height:180px; object-fit:cover; border-radius:10px; }
    .muted { color:#9aa4b2; }
    .row { display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
    input[type=text] { background:#0c0f14; border:1px solid #2a3040; color:#e6e6e6; padding:10px; border-radius:10px; min-width:320px; }
    code { background:#0c0f14; padding:2px 5px; border-radius:6px; border:1px solid #2a3040; }
    small.version { color:#9aa4b2; font-size:12px; }
  </style>
</head>
<body>
  <div class="container">
    <div style="display:flex;justify-content:space-between;flex-wrap:wrap;align-items:baseline;">
      <h1 style="margin:0;">CamStack</h1>
      <small class="version">v1.0.0</small>
    </div>
    {% block content %}{% endblock %}
  </div>
</body>
</html>
EOF

# ------------------------------------------------------------------------------
# 10. app/templates/index.html
# ------------------------------------------------------------------------------
cat > "$CAMROOT/app/templates/index.html" <<"EOF"
{% extends "base.html" %}
{% block content %}

<p class="muted">Device IP: <b>{{ ip }}</b> — Admin UI at <code>https://{{ ip }}/</code></p>

<div class="card" style="margin:14px 0;">
  <div class="row">
    <button class="btn" hx-get="/api/discover" hx-target="#disc" hx-indicator="#spin">Scan LAN for cameras</button>
    <span id="spin" class="muted" style="display:none;">Scanning…</span>
  </div>
  <div id="disc" class="grid" style="margin-top:12px;"></div>
</div>

<div class="card">
  <h3>Set camera by RTSP URL</h3>
  <p class="muted">Paste full RTSP, e.g. <code>rtsp://user:pass@10.0.0.42:554/cam/realmonitor?channel=1&subtype=0</code></p>
  <div class="row">
    <input id="rtsp" type="text" placeholder="rtsp://..." value="{{ current or '' }}">
    <button class="btn" onclick="setManual()">Save & Switch</button>
  </div>
  <p class="muted" style="margin-top:8px;">Current: {{ current or '— none set —' }}</p>
</div>

<script>
  document.body.addEventListener('htmx:beforeRequest', (e)=>{
    if(e.target.id==='disc') document.querySelector('#spin').style.display='inline';
  });
  document.body.addEventListener('htmx:afterRequest', (e)=>{
    if(e.target.id==='disc') document.querySelector('#spin').style.display='none';
  });

  document.addEventListener('htmx:afterOnLoad', (e) => {
    if (e.detail.target.id === 'disc') {
      const cams = JSON.parse(e.detail.xhr.responseText);
      const el = e.detail.target;
      if (!cams.length) { el.innerHTML = '<p class="muted">No ONVIF cameras found.</p>'; return; }
      el.innerHTML = cams.map(c => `
        <div class="card cam-card" id="card-${c.ip.replace(/\\./g,'-')}">
          <div>${c.snapshot ? `<img loading="lazy" id="thumb-${c.ip.replace(/\\./g,'-')}" src="${c.snapshot}">` : `<div class="muted" style="width:320px;height:180px;display:flex;align-items:center;justify-content:center;">No preview</div>`}</div>
          <div style="margin-top:8px; font-size:14px;">
            <div><b>${c.model}</b></div>
            <div class="muted iptext">${c.ip}</div>
            <div style="margin-top:6px;" id="controls-${c.ip.replace(/\\./g,'-')}">
              <button class="btn small" onclick="useCam('${c.rtsp_url || ''}','${c.ip}')">Use this camera</button>
              <button class="btn small" onclick="identifyProgress('${c.ip}')">Identify</button>
              <button class="btn small" onclick="testCredsPrompt('${c.ip}')">Test creds</button>
            </div>
            <div id="progress-${c.ip.replace(/\\./g,'-')}" style="margin-top:8px; display:none;">
              <div style="background:#0c0f14;border:1px solid #2a3040;border-radius:8px;overflow:hidden;width:320px;height:12px;">
                <div id="bar-${c.ip.replace(/\\./g,'-')}" style="height:12px;width:0%;background:#1f6feb;"></div>
              </div>
              <div id="lines-${c.ip.replace(/\\./g,'-')}" class="muted" style="font-size:12px;margin-top:6px;white-space:pre-line;"></div>
            </div>
            <div id="report-${c.ip.replace(/\\./g,'-')}" class="muted" style="margin-top:8px;font-size:13px;"></div>
          </div>
        </div>`).join('');
    }
  });

  async function useCam(rtsp, ip){
    if(!rtsp){
      rtsp = prompt("RTSP URL for "+ip+" (e.g. rtsp://user:pass@"+ip+":554/...):");
      if(!rtsp) return;
    }
    await fetch('/api/set_rtsp', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({rtsp_url: rtsp})});
    alert('Camera set. Player will restart.');
  }

  async function identifyProgress(ip){
    const cardId = ip.replace(/\\./g,'-');
    const pWrap = document.getElementById('progress-' + cardId);
    const bar = document.getElementById('bar-' + cardId);
    const lines = document.getElementById('lines-' + cardId);
    const reportEl = document.getElementById('report-' + cardId);
    pWrap.style.display = 'block';
    bar.style.width = '0%';
    lines.textContent = 'Starting…';

    const res = await fetch('/api/identify_start', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ip})});
    const { job_id } = await res.json();
    if(!job_id){ lines.textContent = 'Failed to start job'; return; }

    const timer = setInterval(async ()=>{
      try{
        const sres = await fetch('/api/job_status/' + job_id);
        const st = await sres.json();
        if(st.error){ lines.textContent = 'Error: ' + st.error; clearInterval(timer); return; }
        bar.style.width = (st.progress || 0) + '%';
        if(st.lines && st.lines.length){ lines.textContent = st.lines.join('\\n'); }
        if(st.status === 'done'){
          clearInterval(timer);
          const j = st.result || {};
          let txt = '';
          if(j.open_ports && j.open_ports.length) txt += 'Open ports: ' + j.open_ports.join(', ') + '\\n';
          if(j.likely_vendors && j.likely_vendors.length) txt += 'Likely: ' + j.likely_vendors.join(', ') + '\\n';
          if(j.http_snapshots && j.http_snapshots.length){
            txt += 'Snapshots: ' + j.http_snapshots.map(s => s.split('/').pop()).join(', ') + '\\n';
            const thumbEl = document.getElementById('thumb-' + cardId);
            if(thumbEl){
              thumbEl.src = j.http_snapshots[0].replace('/opt/camstack/runtime', '/snaps') + '?t=' + Date.now();
            }
          }
          if(j.rtsp_found && j.rtsp_found.length){
            txt += 'RTSP: ' + j.rtsp_found.map(r=>r.url).join('; ') + '\\n';
          }
          reportEl.textContent = txt || 'No findings';
        }
        if(st.status === 'error'){
          clearInterval(timer);
          lines.textContent = 'Error: ' + (st.error || 'unknown');
        }
      }catch(e){
        clearInterval(timer);
        lines.textContent = 'Polling error: ' + e;
      }
    }, 800);
  }

  async function testCredsPrompt(ip){
    const user = prompt("Username for " + ip + " (leave blank to cancel):");
    if(!user) return;
    const pass = prompt("Password for " + ip + " (will be used once to test):");
    if(pass === null) return;
    const cardId = ip.replace(/\\./g,'-');
    const reportEl = document.getElementById('report-' + cardId);
    reportEl.innerText = 'Testing creds…';
    try{
      const res = await fetch('/api/identify_start', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ip, user, password: pass})});
      const { job_id } = await res.json();
      if(!job_id){ reportEl.innerText = 'Failed to start job'; return; }
      const timer = setInterval(async ()=>{
        const st = await (await fetch('/api/job_status/' + job_id)).json();
        if(st.status === 'done'){
          clearInterval(timer);
          const j = st.result || {};
          let txt = '';
          if(j.http_snapshots && j.http_snapshots.length){
            txt += 'Authenticated snapshots: ' + j.http_snapshots.map(s => s.split('/').pop()).join(', ') + '\\n';
            const thumbEl = document.getElementById('thumb-' + cardId);
            if(thumbEl){
              thumbEl.src = j.http_snapshots[0].replace('/opt/camstack/runtime', '/snaps') + '?t=' + Date.now();
            }
          } else {
            txt += 'No authenticated snapshots found.';
          }
          reportEl.innerText = txt;
        }
        if(st.status === 'error'){
          clearInterval(timer);
          reportEl.innerText = 'Error: ' + (st.error || 'unknown');
        }
      }, 900);
    }catch(e){
      reportEl.innerText = 'Test creds failed: ' + e;
    }
  }

  async function setManual(){
    const rtsp = document.getElementById('rtsp').value.trim();
    if(!rtsp) { alert('Please enter an RTSP URL.'); return; }
    await fetch('/api/set_rtsp', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({rtsp_url: rtsp})});
    alert('Camera set. Player will restart.');
  }
</script>
{% endblock %}
EOF

# ------------------------------------------------------------------------------
# 11. services/*.service
# ------------------------------------------------------------------------------
cat > "$CAMROOT/services/camstack.service" <<"EOF"
[Unit]
Description=CamStack FastAPI (HTTPS 443) v1.0.0
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/camstack
ExecStart=/opt/camstack/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 443 --ssl-keyfile /opt/camstack/certs/server.key --ssl-certfile /opt/camstack/certs/server.crt --proxy-headers
Restart=always
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

cat > "$CAMROOT/services/camredirect.service" <<"EOF"
[Unit]
Description=HTTP->HTTPS redirector (port 80) v1.0.0
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/camstack
ExecStart=/opt/camstack/.venv/bin/uvicorn app.redirect_http:app --host 0.0.0.0 --port 80
Restart=always
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

cat > "$CAMROOT/services/camplayer.service" <<"EOF"
[Unit]
Description=Fullscreen camera player (mpv) v1.0.0
After=network-online.target camstack.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/camstack
ExecStart=/opt/camstack/scripts/run_camplayer.sh
Restart=always
RestartSec=2
TimeoutStartSec=30
TimeoutStopSec=10
WatchdogSec=60
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

# ------------------------------------------------------------------------------
# 12. scripts/pki.sh, enable_private_ca.sh, run_camplayer.sh, install_camstack.sh
# ------------------------------------------------------------------------------
cat > "$CAMROOT/scripts/pki.sh" <<"EOF"
#!/usr/bin/env bash
set -euo pipefail
CERT_DIR="/opt/camstack/certs"
CA_DIR="/opt/camstack/ca"
mkdir -p "$CERT_DIR" "$CA_DIR"

ensure_self_signed() {
  if [ -f "$CERT_DIR/server.crt" ] && [ -f "$CERT_DIR/server.key" ]; then
    echo "[*] Self-signed cert already present."
    return 0
  fi
  echo "[*] Generating self-signed server certificate..."
  openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
    -keyout "$CERT_DIR/server.key" \
    -out "$CERT_DIR/server.crt" \
    -subj "/CN=$(hostname -I | awk '{print $1}')/O=CamStack v1.0.0"
  chmod 600 "$CERT_DIR/server.key"
}

ensure_private_ca_local() {
  if [ -f "$CA_DIR/rootCA.crt" ] && [ -f "$CA_DIR/rootCA.key" ]; then
    echo "[*] Local CA already present."
    return 0
  fi
  echo "[*] Creating local CamStack Root CA (stored on this device)."
  openssl genrsa -out "$CA_DIR/rootCA.key" 4096
  openssl req -x509 -new -nodes -key "$CA_DIR/rootCA.key" -sha256 -days 3650 \
    -out "$CA_DIR/rootCA.crt" -subj "/C=US/O=CamStackLAN/CN=CamStack Root CA v1.0.0"
  chmod 600 "$CA_DIR/rootCA.key"
}

sign_with_local_ca() {
  local CN="$1"
  echo "[*] Issuing server certificate for CN=$CN using local CA..."
  openssl genrsa -out "$CERT_DIR/server.key" 2048
  openssl req -new -key "$CERT_DIR/server.key" -out "$CERT_DIR/server.csr" -subj "/CN=$CN"
  cat > "$CERT_DIR/v3.ext" <<EOEXT
basicConstraints=CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names
[alt_names]
DNS.1 = $CN
IP.1 = $(hostname -I | awk '{print $1}')
EOEXT
  openssl x509 -req -in "$CERT_DIR/server.csr" -CA "$CA_DIR/rootCA.crt" -CAkey "$CA_DIR/rootCA.key" \
    -CAcreateserial -out "$CERT_DIR/server.crt" -days 825 -sha256 -extfile "$CERT_DIR/v3.ext"
  rm -f "$CERT_DIR/server.csr" "$CERT_DIR/v3.ext"
  chmod 600 "$CERT_DIR/server.key"
  echo "[*] Server certificate issued."
  echo "[*] Export this CA to your clients and trust it: $CA_DIR/rootCA.crt"
}

generate_csr_only() {
  local CN="$1"
  echo "[*] Generating CSR for CN=$CN (no signing performed)."
  openssl genrsa -out "$CERT_DIR/server.key" 2048
  openssl req -new -key "$CERT_DIR/server.key" -out "$CERT_DIR/server.csr" -subj "/CN=$CN"
  echo "[*] CSR ready at: $CERT_DIR/server.csr"
  echo "    Sign this CSR with your offline CA, save the cert as $CERT_DIR/server.crt and restart camstack.service"
}
EOF
chmod +x "$CAMROOT/scripts/pki.sh"

cat > "$CAMROOT/scripts/enable_private_ca.sh" <<"EOF"
#!/usr/bin/env bash
set -euo pipefail
. /opt/camstack/scripts/pki.sh

CN="${1:-camstack.lan}"

echo "[*] Enabling Private CA mode for CN=$CN"
ensure_private_ca_local
sign_with_local_ca "$CN"
systemctl restart camstack.service
echo "[✓] Private CA mode active. Import CA on clients: /opt/camstack/ca/rootCA.crt"
EOF
chmod +x "$CAMROOT/scripts/enable_private_ca.sh"

cat > "$CAMROOT/scripts/run_camplayer.sh" <<"EOF"
#!/usr/bin/env bash
set -euo pipefail
cd /opt/camstack

# Generate overlay
/opt/camstack/.venv/bin/python -m app.overlay_gen

# Run player with watchdog support
/opt/camstack/.venv/bin/python - <<'PY'
from app.player import launch_rtsp_with_watchdog
import sys
sys.exit(launch_rtsp_with_watchdog())
PY
EOF
chmod +x "$CAMROOT/scripts/run_camplayer.sh"

cat > "$CAMROOT/scripts/install_camstack.sh" <<"EOF"
#!/usr/bin/env bash
set -euo pipefail

TLS_MODE="${CAMSTACK_TLS_MODE:-selfsigned}"  # selfsigned | private_ca_local | csr_only
CN_DEFAULT="${CAMSTACK_CN:-camstack.lan}"    # Common Name for cert

echo "[*] Preparing system packages..."
apt update
apt install -y mpv ffmpeg yt-dlp python3 python3-venv jq git tree curl openssl systemd

mkdir -p /opt/camstack/runtime/snaps /opt/camstack/logs /opt/camstack/certs /opt/camstack/ca
cd /opt/camstack

# Install uv if not present
if ! command -v uv >/dev/null 2>&1; then
  echo "[*] Installing uv (Astral)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

echo "[*] Creating venv & installing Python deps (via uv)..."
uv sync

# TLS setup
. /opt/camstack/scripts/pki.sh

case "$TLS_MODE" in
  selfsigned)
    echo "[*] TLS mode: self-signed"
    ensure_self_signed
    ;;
  private_ca_local)
    echo "[*] TLS mode: local Private CA (on this device)"
    ensure_private_ca_local
    sign_with_local_ca "$CN_DEFAULT"
    ;;
  csr_only)
    echo "[*] TLS mode: CSR-only (offline CA flow)"
    generate_csr_only "$CN_DEFAULT"
    ;;
  *)
    echo "[!] Unknown CAMSTACK_TLS_MODE=$TLS_MODE; defaulting to self-signed"
    ensure_self_signed
    ;;
esac

echo "[*] Installing systemd services..."
cp services/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable camredirect.service camstack.service camplayer.service
systemctl restart camredirect.service camstack.service camplayer.service

echo
echo "[✓] CamStack 1.0.0 is up."
echo "    HTTPS Admin UI: https://$(hostname -I | awk '{print $1}')/"
if [ "$TLS_MODE" = "selfsigned" ]; then
  echo "    (Self-signed cert — browser will warn until you trust it.)"
elif [ "$TLS_MODE" = "private_ca_local" ]; then
  echo "    Trust this CA on clients: /opt/camstack/ca/rootCA.crt"
elif [ "$TLS_MODE" = "csr_only" ]; then
  echo "    CSR ready at: /opt/camstack/certs/server.csr"
  echo "    After you sign it, save as /opt/camstack/certs/server.crt then:"
  echo "      sudo systemctl restart camstack.service"
fi
EOF
chmod +x "$CAMROOT/scripts/install_camstack.sh"

# ------------------------------------------------------------------------------
# 13. top-level installer (install_me.sh) and README.md
# ------------------------------------------------------------------------------
cat > "$ROOT/install_me.sh" <<"EOF"
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/camstack"
DST="/opt/camstack"
echo "[*] Copying files to $DST ..."
mkdir -p "$DST"
cp -a "$SRC/." "$DST/"
echo "[*] Running installer ..."
cd "$DST/scripts"
bash install_camstack.sh
echo "[✓] CamStack 1.0.0 install complete."
EOF
chmod +x "$ROOT/install_me.sh"

cat > "$ROOT/README.md" <<"EOF"
# CamStack 1.0.0

CamStack is a self-contained, offline, single-camera display appliance for x86 thin clients / mini PCs.

## What it does

- Fullscreen live camera playback (mpv).
- On-screen overlay with:
  - CamStack version
  - Device IP
  - Admin URL
- Auto-fallback to a nature livestream if the chosen RTSP feed is down.
- Built-in HTTPS admin UI (FastAPI + HTMX) on port 443:
  - Scan LAN for ONVIF cameras
  - Show snapshot thumbnails
  - Guess vendor and RTSP paths
  - Let you pick "Use this camera"
  - Let you paste a manual RTSP URL
  - Run "Identify" deep probe with a live progress bar
- Port 80 just redirects straight to HTTPS.
- Systemd services so it runs headless as an appliance.
- Offline TLS:
  - selfsigned (default)
  - private_ca_local (generates a local LAN CA you can trust)
  - csr_only (you sign the cert yourself)

## Install

1. Copy `CamStack_1.0.0.zip` to your Ubuntu box.
2. Run:

   unzip CamStack_1.0.0.zip
   cd CamStack_1.0.0
   sudo bash install_me.sh

The installer:
- Copies everything to `/opt/camstack`
- Installs system deps (`mpv`, `ffmpeg`, `yt-dlp`, etc.)
- Sets up Python deps with `uv`
- Generates HTTPS certs
- Installs and starts:
  - `camredirect.service` (HTTP→HTTPS redirect on :80)
  - `camstack.service`    (FastAPI admin on :443 with TLS)
  - `camplayer.service`   (fullscreen RTSP player w/ overlay + fallback)

When it finishes it'll print something like:

    HTTPS Admin UI: https://192.168.86.86/

Open that URL from another device on the same LAN.  
You'll see a browser warning for the self-signed cert. Continue past it and you're in.

## After install

- The TV/monitor plugged into the box will show either:
  - Your chosen camera fullscreen, or
  - The fallback nature stream
- The overlay in the video includes the box's IP and the admin URL, so you (or someone in the field) can just type that into a phone and switch cameras.

## Version / Changelog

### 1.0.0
- First public cut.
- Appliance-style boot.
- HTTPS by default (self-signed).
- Camera discovery + Identify + thumbnail grab.
- Fallback nature cam behavior.
EOF

# ------------------------------------------------------------------------------
# 14. Zip it
# ------------------------------------------------------------------------------
echo "[*] Creating ${ROOT}.zip ..."
zip -r "${ROOT}.zip" "$ROOT" >/dev/null

echo "[✓] Done."
echo
echo "Your bundle is: ${ROOT}.zip"
echo "Next steps:"
echo "  unzip ${ROOT}.zip"
echo "  cd ${ROOT}"
echo "  sudo bash install_me.sh"
