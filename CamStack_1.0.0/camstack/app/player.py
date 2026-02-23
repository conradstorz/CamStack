from __future__ import annotations
from pathlib import Path
import subprocess, json, time, signal, threading, os
from dataclasses import dataclass
from typing import Optional
from loguru import logger
from PIL import Image, ImageTk
from .overlay_gen import write_overlay
from .fallback import (
    get_featured_fallback_url,
    get_best_live_stream,
    LiveStreamInfo,
    load_cached_stream,
    save_cached_stream,
)
from .motion_detector import MotionDetector
from .motion_memory import MotionMemory, DEFAULT_CLIP_DURATION

BASE = Path("/opt/camstack")
CFG = BASE / "runtime/config.json"
OVL = BASE / "runtime/overlay.ass"
SNAP_DIR = BASE / "runtime/snaps"
DEFAULT_STILL = BASE / "runtime/default.jpg"


class StillFrameDisplay:
    """Persistent fullscreen still-frame renderer for HDMI output."""

    def __init__(self):
        import tkinter as tk

        self._tk = tk
        self._root = tk.Tk()
        self._root.configure(bg="black")
        screen_w = self._root.winfo_screenwidth()
        screen_h = self._root.winfo_screenheight()
        self._screen_w = screen_w
        self._screen_h = screen_h
        self._enforce_fullscreen()
        self._root.config(cursor="none")
        self._root.bind("<Escape>", lambda _e: None)

        self._label = tk.Label(self._root, bg="black", borderwidth=0, highlightthickness=0)
        self._label.pack(fill="both", expand=True)

        self._photo = None
        self._width = screen_w
        self._height = screen_h
        self._alive = True
        self._root.update_idletasks()
        self._root.update()
        self._enforce_fullscreen()
        self._refresh_display_size()

    def _enforce_fullscreen(self) -> None:
        self._root.overrideredirect(True)
        self._root.geometry(f"{self._screen_w}x{self._screen_h}+0+0")
        self._root.attributes("-fullscreen", True)
        self._root.attributes("-topmost", True)
        self._root.lift()

    def _refresh_display_size(self) -> None:
        label_w = self._label.winfo_width()
        label_h = self._label.winfo_height()
        root_w = self._root.winfo_width()
        root_h = self._root.winfo_height()

        new_w = label_w if label_w > 1 else root_w
        new_h = label_h if label_h > 1 else root_h

        if new_w > 1:
            self._width = new_w
        if new_h > 1:
            self._height = new_h

    def show_image(self, path: Path) -> bool:
        if not self._alive:
            return False
        try:
            self._enforce_fullscreen()
            self._refresh_display_size()
            frame = Image.open(path).convert("RGB")
            src_w, src_h = frame.size
            if src_w <= 0 or src_h <= 0:
                return False

            scale = max(self._width / src_w, self._height / src_h)
            scaled_w = max(1, int(round(src_w * scale)))
            scaled_h = max(1, int(round(src_h * scale)))

            resized = frame.resize((scaled_w, scaled_h), Image.Resampling.LANCZOS)

            left = max(0, (scaled_w - self._width) // 2)
            top = max(0, (scaled_h - self._height) // 2)
            right = left + self._width
            bottom = top + self._height

            canvas = resized.crop((left, top, right, bottom))
            self._photo = ImageTk.PhotoImage(canvas)
            self._label.configure(image=self._photo)
            self.pump()
            return True
        except Exception as e:
            logger.debug(f"Still-frame render failed for {path}: {e}")
            return False

    def pump(self) -> bool:
        if not self._alive:
            return False
        try:
            self._enforce_fullscreen()
            self._root.update_idletasks()
            self._root.update()
            self._refresh_display_size()
            return True
        except Exception:
            self._alive = False
            return False

    def close(self):
        if not self._alive:
            return
        try:
            self._root.destroy()
        except Exception:
            pass
        self._alive = False


def _safe_camera_id(camera_id: str) -> str:
    return camera_id.replace(".", "_").replace(":", "_").replace("/", "_")


def _annotate_frame(
    src_path: Path,
    text: str,
    output_path: Optional[Path] = None,
) -> Path:
    """
    Render *text* as a semi-transparent banner at the bottom of a JPEG frame
    using PIL.  Returns *output_path* on success, *src_path* on any error so
    the caller always gets a valid image path back.
    """
    from PIL import ImageDraw, ImageFont
    try:
        dest = output_path or src_path
        img = Image.open(src_path).convert("RGB")
        draw = ImageDraw.Draw(img, "RGBA")
        w, h = img.size

        font_size = max(18, h // 22)
        try:
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size
            )
        except Exception:
            font = ImageFont.load_default()

        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        pad = 10

        # Background pill centred at the bottom of the frame
        rx0 = (w - tw) // 2 - pad
        ry0 = h - th - pad * 3
        rx1 = rx0 + tw + pad * 2
        ry1 = h - pad
        draw.rounded_rectangle([rx0, ry0, rx1, ry1], radius=8, fill=(0, 0, 0, 172))
        draw.text(
            ((w - tw) // 2, ry0 + pad // 2),
            text,
            font=font,
            fill=(255, 220, 50, 255),   # warm amber
        )
        img.save(str(dest), "JPEG", quality=85)
        return dest
    except Exception as e:
        logger.debug(f"Frame annotation failed: {e}")
        return src_path


def _play_motion_clip(
    clip_path: Path,
    osd_text: str,
    duration: float,
) -> subprocess.Popen:
    """
    Launch mpv to play a local motion-memory clip fullscreen.
    The *osd_text* is shown as a persistent OSD message while the clip plays.
    Returns the Popen object; caller is responsible for cleanup.
    """
    cmd = [
        "mpv",
        "--hwdec=no", "--fs", "--force-window=yes",
        "--osc=no", "--no-input-default-bindings", "--no-border",
        "--osd-level=2",
        f"--osd-msg1={osd_text}",
        "--osd-font-size=48",
        "--osd-color=#FFFFDC",
        "--osd-back-color=#AA000000",
        f"--length={int(duration)}",
        str(clip_path),
    ]
    return subprocess.Popen(cmd, stderr=subprocess.DEVNULL)


def _grab_display_frame(rtsp_url: str, camera_id: str) -> Optional[Path]:
    """Capture one display-quality still frame from RTSP stream."""
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    snap_path = SNAP_DIR / f"display_{_safe_camera_id(camera_id)}.jpg"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp", "-i", rtsp_url,
        "-frames:v", "1", "-q:v", "4", "-y", str(snap_path),
    ]
    try:
        subprocess.run(cmd, check=True, timeout=4, capture_output=True)
        if snap_path.exists() and snap_path.stat().st_size > 0:
            return snap_path
    except Exception as e:
        logger.debug(f"Display frame capture failed for {camera_id}: {e}")
    return None


def _show_default_still(display: StillFrameDisplay) -> bool:
    """Show operator-provided default fullscreen still, if available."""
    try:
        if DEFAULT_STILL.exists() and DEFAULT_STILL.stat().st_size > 0:
            return display.show_image(DEFAULT_STILL)
    except Exception as e:
        logger.debug(f"Default still render failed: {e}")
    return False

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
    
    # Check if motion detection is enabled
    motion_config = _load_motion_config()
    if motion_config and motion_config.get("enabled", False):
        logger.info("Motion detection enabled - launching multi-camera mode")
        return launch_with_motion_detection(motion_config)
    
    # Standard single-camera mode
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


def _load_motion_config() -> Optional[dict]:
    """Load motion detection configuration from config.json."""
    if not CFG.exists():
        return None
    try:
        cfg = json.loads(CFG.read_text())
        return cfg.get("motion_detection")
    except Exception as e:
        logger.warning(f"Failed to load motion config: {e}")
        return None


def launch_with_motion_detection(motion_config: dict) -> int:
    """
    Launch multi-camera player with motion-based switching.
    
    Monitors all configured cameras for motion and switches display
    to the camera with highest motion intensity. Rotates through
    cameras when no motion is detected.
    """
    logger.info("Starting motion-based multi-camera monitoring")
    
    # Extract configuration
    snapshot_interval = motion_config.get("snapshot_interval", 1.0)
    sensitivity = motion_config.get("sensitivity", 12.0)
    frame_threshold = motion_config.get("frame_threshold", 3)
    rotation_interval = motion_config.get("rotation_interval", 20)
    cameras = motion_config.get("cameras", {})
    
    if not cameras:
        logger.error("No cameras configured for motion detection")
        return launch_rtsp_with_watchdog()  # Fall back to single camera mode
    
    # Initialize motion detector
    detector = MotionDetector(
        snapshot_interval=snapshot_interval,
        sensitivity=sensitivity,
        frame_threshold=frame_threshold,
    )
    
    # Add all enabled cameras
    enabled_cameras = []
    for camera_id, cam_cfg in cameras.items():
        if cam_cfg.get("enabled", True):
            rtsp_url = cam_cfg.get("rtsp_url")
            if rtsp_url:
                detector.add_camera(camera_id, rtsp_url, enabled=True)
                enabled_cameras.append((camera_id, rtsp_url))
                logger.info(f"Added camera {camera_id} to rotation")
    
    if not enabled_cameras:
        logger.error("No enabled cameras with RTSP URLs")
        return 1
    
    # Motion memory (NVR-like clip retention)
    motion_memory = MotionMemory(
        clip_duration=motion_config.get("clip_duration", DEFAULT_CLIP_DURATION)
    )
    prev_motion_camera_id: Optional[str] = None
    # Lookup dict for fast url resolution during motion rising-edge handler
    _camera_url_map: dict[str, str] = {cam_id: url for cam_id, url in enabled_cameras}

    # State tracking
    current_camera_idx = 0
    current_camera_id, current_rtsp_url = enabled_cameras[current_camera_idx]
    last_rotation = time.monotonic()
    last_motion_camera = None
    motion_mode = False
    display_interval = max(0.15, min(1.0, snapshot_interval))
    last_display_update = 0.0
    last_frame_path: Optional[Path] = None
    last_successful_frame_at = time.monotonic()
    frame_fail_counts: dict[str, int] = {cam_id: 0 for cam_id, _ in enabled_cameras}
    all_offline_fail_threshold = 1
    offline_frame_timeout = max(12.0, (rotation_interval * len(enabled_cameras)) + 3.0)

    try:
        display = StillFrameDisplay()
    except Exception as e:
        logger.warning(f"Still-frame display unavailable, falling back to mpv switching: {e}")
        display = None
    display_capable = display is not None
    realtime_motion_mode = False
    
    # Start motion detection background updates only when renderer is active.
    detector.start_monitoring()

    # Fallback path keeps previous behavior if GUI display cannot be created.
    if display is None:
        write_overlay(False)
        procs, primary, files = _spawn_player(current_rtsp_url)
        logger.info(f"Displaying camera {current_camera_id}: {current_rtsp_url}")
    else:
        logger.info(f"Displaying camera {current_camera_id} using still-frame renderer")
    
    try:
        while True:
            now = time.monotonic()
            
            # Keep GUI responsive.
            if display is not None and not display.pump():
                logger.warning("Still-frame GUI closed, switching to mpv fallback")
                display = None
                write_overlay(False)
                procs, primary, files = _spawn_player(current_rtsp_url)

            # Check for player crash in fallback mode.
            if display is None and primary.poll() is not None:
                logger.warning(f"Player crashed for camera {current_camera_id}, advancing...")
                _terminate_procs(procs)
                _close_files(files)

                current_camera_idx = (current_camera_idx + 1) % len(enabled_cameras)
                current_camera_id, current_rtsp_url = enabled_cameras[current_camera_idx]
                last_rotation = now

                write_overlay(False)
                procs, primary, files = _spawn_player(current_rtsp_url)
                logger.info(f"Switched to camera {current_camera_id}")
                time.sleep(2)
                continue
            
            # Check for motion
            motion_camera_id = detector.check_all_cameras()

            # Rising-edge: a camera that was not firing motion just started.
            # Trigger clip recording immediately so it captures the live event.
            if motion_camera_id and motion_camera_id != prev_motion_camera_id:
                cam_url_for_rec = _camera_url_map.get(motion_camera_id)
                if cam_url_for_rec:
                    cam_score = (
                        detector.get_camera_states()
                        .get(motion_camera_id, {})
                        .get("motion_score", 0.0)
                    ) / 100.0
                    motion_memory.record_clip(motion_camera_id, cam_url_for_rec, cam_score)
            prev_motion_camera_id = motion_camera_id

            # If all monitored cameras have failed/been disabled, stop showing
            # stale camera imagery and switch to nature fallback behavior.
            camera_states = detector.get_camera_states()
            all_cameras_offline = bool(camera_states) and all(
                (not st.get("enabled", True)) for st in camera_states.values()
            )
            all_snapshots_failing = bool(frame_fail_counts) and all(
                count >= all_offline_fail_threshold for count in frame_fail_counts.values()
            )
            frame_timeout_exceeded = (now - last_successful_frame_at) >= offline_frame_timeout

            if all_cameras_offline or all_snapshots_failing or frame_timeout_exceeded:
                logger.warning("All motion cameras appear offline; switching to fallback stream")
                if display is not None:
                    display.close()
                    display = None
                else:
                    _terminate_procs(procs)
                    _close_files(files)
                return _fallback_loop()

            # Still-frame mode is lightweight but cannot sustain high FPS.
            # Switch to realtime player mode only while motion is active.
            if display is not None and motion_camera_id and display_capable and not realtime_motion_mode:
                logger.info(
                    "Motion detected; switching to realtime playback mode "
                    "for higher frame rate"
                )
                display.close()
                display = None
                write_overlay(False)
                procs, primary, files = _spawn_player(current_rtsp_url)
                realtime_motion_mode = True
            
            if motion_camera_id and motion_camera_id != current_camera_id:
                # Motion detected on different camera - switch immediately
                logger.info(f"Motion detected on camera {motion_camera_id}, switching...")
                if display is None:
                    _terminate_procs(procs)
                    _close_files(files)
                
                # Find the camera
                motion_rtsp_url = None
                for i, (cam_id, rtsp_url) in enumerate(enabled_cameras):
                    if cam_id == motion_camera_id:
                        motion_rtsp_url = rtsp_url
                        current_camera_idx = i
                        break
                
                if motion_rtsp_url:
                    current_camera_id = motion_camera_id
                    current_rtsp_url = motion_rtsp_url
                    last_motion_camera = motion_camera_id
                    motion_mode = True
                    last_rotation = now

                    if display is None:
                        write_overlay(False)
                        procs, primary, files = _spawn_player(current_rtsp_url)
                    logger.info(f"Now displaying motion camera: {current_camera_id}")
                
            elif motion_camera_id == current_camera_id:
                # Still motion on current camera - stay here
                last_rotation = now
                motion_mode = True
                
            elif motion_mode and not motion_camera_id:
                # Motion stopped - resume rotation after a short delay
                if now - last_rotation > 5:  # 5 second grace period
                    logger.info("Motion stopped, resuming rotation")
                    motion_mode = False
                    last_rotation = now

                    # Return to still-frame mode after motion so idle rotation
                    # does not tear down fullscreen video between cameras.
                    if realtime_motion_mode and display_capable and display is None:
                        try:
                            display = StillFrameDisplay()
                            _show_default_still(display)
                            _terminate_procs(procs)
                            _close_files(files)
                            realtime_motion_mode = False
                            logger.info("Returned to still-frame mode after motion")
                        except Exception as e:
                            logger.warning(
                                "Failed to restore still-frame mode; staying in realtime mode: "
                                f"{e}"
                            )
                            _terminate_procs(procs)
                            _close_files(files)
                            write_overlay(False)
                            procs, primary, files = _spawn_player(current_rtsp_url)
            
            # Rotation logic (only when no motion)
            if not motion_mode and (now - last_rotation) >= rotation_interval:
                # Time to rotate to next camera
                if display is None:
                    _terminate_procs(procs)
                    _close_files(files)

                current_camera_idx = (current_camera_idx + 1) % len(enabled_cameras)
                current_camera_id, current_rtsp_url = enabled_cameras[current_camera_idx]
                last_rotation = now
                last_frame_path = None  # invalidate stale cache for new camera

                if display is None:
                    write_overlay(False)
                    procs, primary, files = _spawn_player(current_rtsp_url)
                logger.info(f"Rotated to camera {current_camera_id}")

                # --- Motion Memory: play last clip for this camera ---
                # Only in still-frame mode so we don't fight mpv processes.
                if display is not None:
                    mem_entry = motion_memory.get_last_motion(current_camera_id)
                    if mem_entry:
                        clip_path = Path(mem_entry["clip_path"])
                        ago_text = motion_memory.time_since_motion(current_camera_id) or ""
                        osd_msg = f"{current_camera_id}  \u2022  Last motion: {ago_text}"
                        logger.info(
                            f"[MotionMemory] Playing last clip for {current_camera_id} "
                            f"({ago_text}): {clip_path.name}"
                        )
                        # Temporarily suspend PIL display while clip plays
                        display.close()
                        display = None
                        clip_proc = _play_motion_clip(
                            clip_path, osd_msg, motion_memory.clip_duration
                        )
                        clip_deadline = (
                            time.monotonic() + motion_memory.clip_duration + 2.0
                        )
                        # Poll for clip end or new motion while clip plays
                        while (
                            clip_proc.poll() is None
                            and time.monotonic() < clip_deadline
                        ):
                            chk = detector.check_all_cameras()
                            if chk:
                                # New motion – abort clip, let main loop handle it
                                motion_camera_id = chk
                                break
                            time.sleep(0.1)
                        _terminate_proc(clip_proc)
                        # Restore PIL display unless motion just fired
                        if not motion_camera_id and display_capable:
                            try:
                                display = StillFrameDisplay()
                            except Exception as exc:
                                logger.warning(
                                    f"Could not reopen still display after clip: {exc}"
                                )
                                display = None

            # Render still frames when GUI mode is available.
            if display is not None and (now - last_display_update) >= display_interval:
                frame_path = _grab_display_frame(current_rtsp_url, current_camera_id)
                if frame_path is not None:
                    frame_fail_counts[current_camera_id] = 0
                    last_successful_frame_at = now
                    # Annotate with motion-age text when memory is available
                    ago = motion_memory.time_since_motion(current_camera_id)
                    if ago:
                        ann_path = SNAP_DIR / f"annotated_{_safe_camera_id(current_camera_id)}.jpg"
                        frame_path = _annotate_frame(frame_path, f"Last motion: {ago}", ann_path)
                    if display.show_image(frame_path):
                        last_frame_path = frame_path
                else:
                    frame_fail_counts[current_camera_id] = (
                        frame_fail_counts.get(current_camera_id, 0) + 1
                    )
                    if last_frame_path is not None:
                        # Re-annotate the cached frame (age text changes every second)
                        ago = motion_memory.time_since_motion(current_camera_id)
                        if ago:
                            ann_path = SNAP_DIR / f"annotated_{_safe_camera_id(current_camera_id)}.jpg"
                            show_path = _annotate_frame(
                                last_frame_path, f"Last motion: {ago}", ann_path
                            )
                        else:
                            show_path = last_frame_path
                        display.show_image(show_path)
                last_display_update = now
            
            time.sleep(0.05)
            
    except KeyboardInterrupt:
        logger.info("Motion detection interrupted by user")
    except Exception as e:
        logger.exception(f"Motion detection error: {e}")
    finally:
        detector.stop_monitoring()
        if display is not None:
            display.close()
        else:
            _terminate_procs(procs)
            _close_files(files)
    
    return 0
