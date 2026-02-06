from __future__ import annotations
from pathlib import Path
import subprocess, json
from dataclasses import dataclass
from loguru import logger
from .overlay_gen import write_overlay
from .fallback import (
    get_featured_fallback_url,
    get_best_live_stream,
    LiveStreamInfo,
    load_cached_stream,
    save_cached_stream,
)

BASE = Path("/opt/camstack")
CFG = BASE / "runtime/config.json"
OVL = BASE / "runtime/overlay.ass"

def run_player_once(url: str) -> int:
    write_overlay(False)
    procs, primary, files = _spawn_player(url)
    logger.info(f"Launching mpv: {url}")
    rc = primary.wait()
    _terminate_procs(procs)
    _close_files(files)
    return rc

def _build_mpv_cmd(url: str, use_ytdl: bool = True) -> list[str]:
    cmd = [
        "mpv", "--hwdec=no", "--fs", "--force-window=yes", "--osc=no",
        "--no-input-default-bindings", f"-sub-file={OVL}", "--sid=1",
        "--no-border", "-msg-level=all=info,ffmpeg=info",
        "--log-file=/opt/camstack/runtime/mpv-debug.log",
        "--network-timeout=15", "--rtsp-transport=tcp",
        "--demuxer-max-bytes=32MiB", "--cache-secs=10",
        "--demuxer-readahead-secs=5",
    ]
    if use_ytdl:
        cmd.extend(
            [
                "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.93 Safari/537.36",
                "--referrer=https://www.youtube.com/",
                "--http-header-fields=User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.93 Safari/537.36",
                "--http-header-fields=Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "--http-header-fields=Accept-Language: en-us,en;q=0.5",
                "--http-header-fields=Sec-Fetch-Mode: navigate",
                "--http-header-fields=Referer: https://www.youtube.com/",
                "--http-header-fields=Origin: https://www.youtube.com",
                "--script-opts=ytdl_hook-ytdl_path=yt-dlp",
                "--ytdl-format=best[height<=720]",
                "--ytdl-raw-options=force-ipv4=yes",
                "--ytdl-raw-options=extractor-args=youtube:player_client=android",
            ]
        )
    cmd.append(url)
    return cmd

def _is_youtube_url(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url

def _spawn_player(url: str) -> tuple[list[subprocess.Popen], subprocess.Popen]:
    if _is_youtube_url(url):
        log_path = BASE / "runtime" / "ytdlp.log"
        log_file = open(log_path, "a", encoding="utf-8")
        yt_cmd = [
            "yt-dlp",
            "--no-progress",
            "--downloader-args", "ffmpeg:-loglevel error",
            "--extractor-args", "youtube:player_client=android",
            "--format", "best[height<=720]",
            "-o", "-",
            url,
        ]
        yt_proc = subprocess.Popen(yt_cmd, stdout=subprocess.PIPE, stderr=log_file)
        mpv_cmd = _build_mpv_cmd("-", use_ytdl=False)
        mpv_proc = subprocess.Popen(mpv_cmd, stdin=yt_proc.stdout, stderr=subprocess.DEVNULL)
        if yt_proc.stdout:
            yt_proc.stdout.close()
        return [yt_proc, mpv_proc], mpv_proc, [log_file]
    mpv_proc = subprocess.Popen(_build_mpv_cmd(url), stderr=subprocess.DEVNULL)
    return [mpv_proc], mpv_proc, []

def _terminate_proc(proc: subprocess.Popen, timeout: int = 10) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass

def _terminate_procs(procs: list[subprocess.Popen]) -> None:
    for proc in procs:
        if proc.poll() is None:
            _terminate_proc(proc)

def _close_files(files: list[object]) -> None:
    for handle in files:
        try:
            handle.close()
        except Exception:
            pass

def _fallback_loop() -> int:
    import time

    write_overlay(True)
    blocked: set[str] = set()
    current = load_cached_stream()
    if current is None:
        current = LiveStreamInfo(url=get_featured_fallback_url(), title=None, viewers=0)

    logger.warning(
        f"Fallback stream selected: {current.url} (viewers={current.viewers})"
    )
    procs, primary, files = _spawn_player(current.url)
    last_check = time.monotonic()

    # Try to rank and switch soon after startup.
    try:
        best = get_best_live_stream(exclude=blocked)
    except Exception as e:
        logger.warning(f"Ranking failed: {e}")
        best = None
    if best and best.viewers > current.viewers and best.url != current.url:
        logger.warning(
            "Switching fallback stream: "
            f"{current.viewers} -> {best.viewers} viewers"
        )
        _terminate_procs(procs)
        _close_files(files)
        current = best
        write_overlay(True)
        procs, primary, files = _spawn_player(current.url)
    if best:
        save_cached_stream(best)

    while True:
        try:
            if primary.poll() is not None:
                blocked.add(current.url)
                logger.warning("Fallback stream failed; selecting a new candidate")
                try:
                    best = get_best_live_stream(exclude=blocked)
                except Exception as e:
                    logger.warning(f"Ranking failed: {e}")
                    best = None
                if best is None:
                    current = LiveStreamInfo(url=get_featured_fallback_url(), title=None, viewers=0)
                else:
                    current = best
                    save_cached_stream(best)
                write_overlay(True)
                _terminate_procs(procs)
                _close_files(files)
                procs, primary, files = _spawn_player(current.url)
                last_check = time.monotonic()
                continue
        except Exception as e:
            logger.warning(f"Fallback loop error: {e}")
            time.sleep(2)
            continue

        now = time.monotonic()
        if now - last_check >= 300:
            try:
                best = get_best_live_stream(exclude=blocked)
            except Exception as e:
                logger.warning(f"Ranking failed: {e}")
                best = None
            if best and best.viewers > current.viewers and best.url != current.url:
                logger.warning(
                    "Switching fallback stream: "
                    f"{current.viewers} -> {best.viewers} viewers"
                )
                _terminate_procs(procs)
                _close_files(files)
                current = best
                write_overlay(True)
                procs, primary, files = _spawn_player(current.url)
                save_cached_stream(best)
            last_check = now

        time.sleep(1)

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
    
    # Fallback to nature cam with ranking refresh
    logger.warning("RTSP missing or failed; switching to fallback nature cam")
    return _fallback_loop()
