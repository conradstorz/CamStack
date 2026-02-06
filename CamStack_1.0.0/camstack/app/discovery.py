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

def _create_onvif_camera(ip: str, user: str, password: str) -> ONVIFCamera:
    try:
        return ONVIFCamera(ip, 80, user, password, wsdl=None, encrypt=False)
    except TypeError:
        return ONVIFCamera(ip, 80, user, password, encrypt=False)

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
            cam = _create_onvif_camera(ip, "", "")
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
