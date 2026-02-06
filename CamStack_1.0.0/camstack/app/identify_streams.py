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
    "rtsp://{ip}:8554/live.sdp",
    "rtsp://{ip}:554/Streaming/Channels/101",
    "rtsp://{ip}:554/Streaming/Channels/102",
    "rtsp://{ip}:8554/Streaming/Channels/101",
    "rtsp://{ip}:8554/Streaming/Channels/102",
    "rtsp://{ip}/Streaming/Channels/101",
    "rtsp://{ip}:8554/Streaming/Channels/101",
    "rtsp://{ip}/h264/ch1/main/av_stream",
    "rtsp://{ip}:8554/h264/ch1/main/av_stream",
    "rtsp://{ip}/ISAPI/Streaming/Channels/101",
    "rtsp://{ip}:8554/ISAPI/Streaming/Channels/101",
    "rtsp://{ip}/cam/realmonitor?channel=1&subtype=0",
    "rtsp://{ip}:554/cam/realmonitor?channel=1&subtype=0",
    "rtsp://{ip}:8554/cam/realmonitor?channel=1&subtype=0",
    "rtsp://{ip}/axis-media/media.amp",
    "rtsp://{ip}:8554/axis-media/media.amp",
    "rtsp://{ip}/h264Preview_01_main",
    "rtsp://{ip}:8554/h264Preview_01_main",
    "rtsp://{ip}/videoMain", "rtsp://{ip}/videoSub",
    "rtsp://{ip}:554/stream1", "rtsp://{ip}:554/stream2",
    "rtsp://{ip}:8554/stream1", "rtsp://{ip}:8554/stream2",
    "rtsp://{ip}/unicast", "rtsp://{ip}/rtsp/1",
    "rtsp://{ip}:8554/unicast", "rtsp://{ip}:8554/rtsp/1",
    "rtsp://{ip}/LiveMedia/stream1",
    "rtsp://{ip}:8554/LiveMedia/stream1",
    "rtsp://{ip}/mediaportal/stream1",
    "rtsp://{ip}:8554/mediaportal/stream1",
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

def _create_onvif_camera(ip: str, user: str, password: str) -> "ONVIFCamera":
    try:
        return ONVIFCamera(ip, 80, user, password, wsdl=None, encrypt=False)
    except TypeError:
        return ONVIFCamera(ip, 80, user, password, encrypt=False)

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
        cam = _create_onvif_camera(ip, user or "", passwd or "")
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
