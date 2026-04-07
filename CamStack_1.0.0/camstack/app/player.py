from __future__ import annotations
from pathlib import Path
import subprocess, json, time, signal, threading, os
from dataclasses import dataclass
from typing import Optional
from loguru import logger
import numpy as np
import cv2
from PIL import Image, ImageTk
from .overlay_gen import write_overlay, get_first_ipv4, VERSION
from .fallback import (
    get_featured_fallback_url,
    get_best_live_stream,
    get_reddit_nature_cams,
    LiveStreamInfo,
    load_cached_stream,
    save_cached_stream,
    EXPLORE_LIVE_URLS,
)
from .motion_detector import MotionDetector
from .motion_memory import MotionMemory, DEFAULT_CLIP_DURATION

BASE = Path("/opt/camstack")
CFG = BASE / "runtime/config.json"
OVL = BASE / "runtime/overlay.ass"
SNAP_DIR = BASE / "runtime/snaps"
DEFAULT_STILL = BASE / "runtime/default.jpg"
CAMERA_RECOVERED = 75   # sentinel: _fallback_loop returns this when cameras come back online


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

    def show_black(self) -> None:
        """Fill the window with solid black (use before any transition)."""
        if not self._alive:
            return
        try:
            black = Image.new("RGB", (max(1, self._width), max(1, self._height)), (0, 0, 0))
            self._photo = ImageTk.PhotoImage(black)
            self._label.configure(image=self._photo)
            self.pump()
        except Exception as e:
            logger.debug(f"show_black failed: {e}")

    def show_np_frame(self, frame: np.ndarray) -> bool:
        """Render a BGR numpy array (from cv2) directly to the display window."""
        if not self._alive:
            return False
        try:
            self._enforce_fullscreen()
            self._refresh_display_size()
            rgb = frame[:, :, ::-1]
            pil_img = Image.fromarray(rgb.astype("uint8"))
            resized = pil_img.resize(
                (max(1, self._width), max(1, self._height)),
                Image.Resampling.BILINEAR,
            )
            self._photo = ImageTk.PhotoImage(resized)
            self._label.configure(image=self._photo)
            self.pump()
            return True
        except Exception as e:
            logger.debug(f"show_np_frame failed: {e}")
            return False


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
        _font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        def _make_font(size: int):
            try:
                return ImageFont.truetype(_font_path, size)
            except Exception:
                return ImageFont.load_default()

        font = _make_font(font_size)
        pad = 10
        h_margin = max(pad * 4, w // 16)   # horizontal safe zone on each side
        max_tw = w - h_margin * 2

        # Shrink font until the label fits within the safe horizontal width
        while font_size > 10:
            bbox = draw.textbbox((0, 0), text, font=font)
            if (bbox[2] - bbox[0]) <= max_tw:
                break
            font_size -= 2
            font = _make_font(font_size)

        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        # Keep banner well clear of the bottom edge so it isn't clipped when
        # tkinter letterboxes or the display overscan trims the frame.
        bottom_margin = max(pad * 4, h // 14)  # ≥6% of frame height

        # Background pill centred at the bottom of the frame
        rx0 = (w - tw) // 2 - pad
        ry1 = h - bottom_margin
        ry0 = ry1 - th - pad * 2
        rx1 = rx0 + tw + pad * 2
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


def _play_clip_as_stills(
    clip_path: Path,
    display: "StillFrameDisplay",
    annotation: str = "",
    fps: int = 8,
    speed: float = 1.0,
    abort_check=None,
) -> None:
    """
    Decode a recorded mp4 clip to JPEG frames and render via StillFrameDisplay.
    No mpv spawned. No window teardown. Desktop never exposed.
    abort_check is an optional callable() -> bool; return True to stop early.
    speed > 1.0 accelerates playback (e.g. 2.0 = double speed).
    """
    import tempfile, shutil
    tmp = Path(tempfile.mkdtemp(prefix="camclip_"))
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-i", str(clip_path),
                "-r", str(fps),
                str(tmp / "f%05d.jpg"),
            ],
            timeout=30,
            capture_output=True,
        )
        if result.returncode != 0:
            logger.warning(f"[ClipStills] ffmpeg decode failed for {clip_path.name}")
            return
        frames = sorted(tmp.glob("f*.jpg"))
        if not frames:
            logger.warning(f"[ClipStills] No frames decoded from {clip_path.name}")
            return
        effective_speed = max(0.25, float(speed))
        frame_interval = 1.0 / (fps * effective_speed)
        for frame in frames:
            if abort_check and abort_check():
                logger.debug("[ClipStills] Aborted early — live motion detected")
                break
            out = _annotate_frame(frame, annotation) if annotation else frame
            display.show_image(out)
            time.sleep(frame_interval)
    except Exception as e:
        logger.warning(f"[ClipStills] Error playing {clip_path.name}: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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


def _resolve_ytdlp_url(youtube_url: str) -> Optional[str]:
    """Resolve a YouTube URL to a direct streamable URL via yt-dlp."""
    try:
        result = subprocess.run(
            [
                "yt-dlp",
                "--format", "best[height<=1080]/best",
                "--extractor-args", "youtube:player_client=android",
                "--no-playlist",
                "-g", youtube_url,
            ],
            capture_output=True, text=True, timeout=45,
        )
        if result.returncode == 0:
            url = result.stdout.strip().split("\n")[0]
            return url or None
    except Exception as e:
        logger.warning(f"[yt-dlp] URL resolution failed: {e}")
    return None


# Words that disqualify a stream from being used as the ambient nature feed.
_NATURE_REJECT: frozenset[str] = frozenset({
    "music", "song", "opera", "concert", "album", "band", "singer",
    "vocals", "lyrics", "playlist", "soundtrack", "pavarotti", "classical",
    "gaming", "game", "minecraft", "fortnite", "twitch", "esport",
    "movie", "film", "trailer", "comedy", "funny", "meme",
    "news", "politics", "sports", "football", "basketball",
    "jellyfish", "monterey",
})


def _resolve_ytdlp_with_title(youtube_url: str, timeout: int = 50) -> Optional[tuple[str, str]]:
    """
    Resolve a YouTube URL to a direct stream URL **and** fetch its title in a
    single yt-dlp invocation.  Returns ``(direct_url, title)`` or ``None``.
    """
    import re as _re
    try:
        result = subprocess.run(
            [
                "yt-dlp", "--no-warnings",
                "--format", "best[height<=1080]/best",
                "--extractor-args", "youtube:player_client=android",
                "--no-playlist", "-J", youtube_url,
            ],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        data = json.loads(result.stdout)
        title: str = data.get("title") or ""
        # Reject non-nature / non-live content based on title words.
        words = set(_re.findall(r"[a-z]+", title.lower()))
        if words & _NATURE_REJECT:
            logger.warning(f"[NatureGrabber] Title rejected: {title!r}")
            return None
        # Extract the direct playback URL from the JSON.
        # Try multiple yt-dlp JSON fields in priority order.
        url: Optional[str] = None
        for rf in (data.get("requested_formats") or []):
            url = rf.get("url")
            if url:
                break
        if not url:
            url = data.get("url")
        if not url:
            # Fall back: pick the best format by height from the formats list.
            fmts = [f for f in (data.get("formats") or []) if f.get("url")]
            candidates = [f for f in fmts if (f.get("height") or 9999) <= 1080] or fmts
            if candidates:
                url = max(candidates, key=lambda f: f.get("height") or 0).get("url")
        return (url, title) if url else None
    except Exception as exc:
        logger.warning(f"[NatureGrabber] resolve+title failed for {youtube_url}: {exc}")
        return None


class NatureGrabber:
    """
    Background thread that reads frames from a nature live stream.

    Resolves a YouTube URL to a direct stream via yt-dlp, then reads
    frames using cv2.VideoCapture.  ``latest_frame`` always holds the
    most recent BGR numpy array, or ``None`` while buffering.
    """

    _REFRESH_INTERVAL: float = 1800.0   # re-resolve stream URL every 30 min
    _IDLE_FPS: float = 30.0              # matches the 30 Hz ambient display loop

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame: Optional[np.ndarray] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="nature-grabber"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def latest_frame(self) -> Optional[np.ndarray]:
        with self._lock:
            return self._frame.copy() if self._frame is not None else None

    def _run(self) -> None:
        cap = None
        last_resolve: float = 0.0
        direct_url: Optional[str] = None
        frame_interval = 1.0 / self._IDLE_FPS

        while not self._stop_event.is_set():
            now = time.monotonic()

            # Resolve / refresh the direct stream URL when needed.
            if direct_url is None or (now - last_resolve) >= self._REFRESH_INTERVAL:
                import random as _random
                resolved_url: Optional[str] = None
                # Shuffle the candidate pool and try each until one passes
                # content validation (title must not match _NATURE_REJECT).
                pool = list(EXPLORE_LIVE_URLS) + get_reddit_nature_cams()
                _random.shuffle(pool)
                for candidate in pool:
                    result = _resolve_ytdlp_with_title(candidate)
                    if result is None:
                        continue
                    url_candidate, title_candidate = result
                    resolved_url = url_candidate
                    logger.info(f"[NatureGrabber] Selected stream: {title_candidate!r}")
                    break

                if resolved_url:
                    if cap is not None:
                        cap.release()
                        cap = None
                    direct_url = resolved_url
                    last_resolve = now
                    cap = cv2.VideoCapture(direct_url)
                    # Larger buffer smooths over HLS segment boundaries
                    # (~2-second segments on YouTube live streams).
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 4)
                    logger.info(f"[NatureGrabber] Stream opened: {direct_url[:80]}\u2026")
                else:
                    logger.warning("[NatureGrabber] All candidates failed or rejected; retrying in 60 s")
                    self._stop_event.wait(60)
                    continue

            if cap is None or not cap.isOpened():
                direct_url = None
                self._stop_event.wait(10)
                continue

            ret, frame = cap.read()
            if not ret:
                logger.warning("[NatureGrabber] Frame read failed \u2014 reconnecting")
                cap.release()
                cap = None
                direct_url = None
                continue

            with self._lock:
                self._frame = frame

            self._stop_event.wait(frame_interval)

        if cap is not None:
            cap.release()
        logger.debug("[NatureGrabber] Thread exited")


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
        "mpv", "--hwdec=auto", "--fs", "--force-window=yes", "--osc=no",
        "--no-input-default-bindings", f"-sub-file={OVL}", "--sid=1",
        "--no-border", "-msg-level=all=info,ffmpeg=info",
        "--log-file=/opt/camstack/runtime/mpv-debug.log",
        "--network-timeout=15", "--rtsp-transport=tcp",
        "--demuxer-max-bytes=64MiB", "--cache-secs=30",
        "--demuxer-readahead-secs=10",
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
                "--ytdl-format=best[height<=1080]/best",
                "--ytdl-raw-options=force-ipv4=yes,extractor-args=youtube:player_client=android",
            ]
        )
    cmd.append(url)
    return cmd

def _is_youtube_url(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url

def _spawn_player(url: str) -> tuple[list[subprocess.Popen], subprocess.Popen, list]:
    if _is_youtube_url(url):
        # Resolve to a direct streamable URL first so mpv doesn't time out
        # probing an empty stdin pipe while yt-dlp's internal ffmpeg starts up.
        direct_url = _resolve_ytdlp_url(url)
        if direct_url:
            logger.info(f"[Player] Resolved {url[:60]}... -> direct stream")
            mpv_proc = subprocess.Popen(
                _build_mpv_cmd(direct_url, use_ytdl=False),
                stderr=subprocess.DEVNULL,
            )
            return [mpv_proc], mpv_proc, []
        # Resolution failed — let mpv use its own ytdl-hook as a fallback.
        logger.warning("[Player] Direct URL resolution failed; trying mpv ytdl-hook")
        mpv_proc = subprocess.Popen(_build_mpv_cmd(url, use_ytdl=True), stderr=subprocess.DEVNULL)
        return [mpv_proc], mpv_proc, []
    # Non-YouTube URL (e.g. RTSP): no ytdl options needed.
    mpv_proc = subprocess.Popen(_build_mpv_cmd(url, use_ytdl=False), stderr=subprocess.DEVNULL)
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

def _probe_any_rtsp(camera_urls: list[str], timeout: int = 5) -> bool:
    """Return True if at least one RTSP URL responds with a valid video frame."""
    for url in camera_urls:
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-rtsp_transport", "tcp",
                    "-i", url,
                    "-frames:v", "1",
                    "-f", "null", "-",
                ],
                timeout=timeout,
                capture_output=True,
            )
            if result.returncode == 0:
                return True
        except Exception:
            pass
    return False


def _fallback_loop(recover_urls: list[str] | None = None) -> int:
    import time

    _RECOVERY_INTERVAL = 60   # seconds between camera probe attempts
    last_recovery_check: float = 0.0   # zero forces an immediate first probe

    write_overlay(True)
    blocked: set[str] = set()
    current = load_cached_stream()
    if current is None:
        current = LiveStreamInfo(url=get_featured_fallback_url(exclude=blocked), title=None, viewers=0)

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
                time.sleep(3)  # brief backoff to prevent rapid crash loops
                try:
                    best = get_best_live_stream(exclude=blocked)
                except Exception as e:
                    logger.warning(f"Ranking failed: {e}")
                    best = None
                if best is None:
                    # Filter blocked URLs from the random pool to avoid re-selecting
                    # a stream that just crashed.
                    fallback_url = get_featured_fallback_url(exclude=blocked)
                    current = LiveStreamInfo(url=fallback_url, title=None, viewers=0)
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

        # Camera recovery probe: periodically test if live streams are reachable.
        now = time.monotonic()
        if recover_urls and (now - last_recovery_check) >= _RECOVERY_INTERVAL:
            last_recovery_check = now
            if _probe_any_rtsp(recover_urls):
                logger.info("[Fallback] Camera(s) back online — returning to live mode")
                _terminate_procs(procs)
                _close_files(files)
                return CAMERA_RECOVERED

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
    
    # Launch multi-camera ambient display whenever cameras are configured.
    # motion_detection.enabled only controls recording behaviour, not the display.
    motion_config = _load_motion_config()
    if motion_config and motion_config.get("cameras"):
        logger.info("Cameras configured — launching multi-camera ambient display")
        while True:
            rc = launch_with_motion_detection(motion_config)
            if rc != CAMERA_RECOVERED:
                return rc
            logger.info("Cameras back online — re-entering multi-camera display")
            motion_config = _load_motion_config() or motion_config
    
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


def _start_frame_grabbers(
    cameras: list[tuple[str, str]],
    interval: float,
    stop_event: threading.Event,
    cache: dict,
) -> list[threading.Thread]:
    """
    Start one background ffmpeg frame-grab thread per camera.
    Each thread writes ``(path, grab_timestamp)`` into *cache[camera_id]*.
    Non-blocking: main loop reads from the cache without waiting on ffmpeg.
    """
    def _grabber(camera_id: str, rtsp_url: str) -> None:
        while not stop_event.is_set():
            path = _grab_display_frame(rtsp_url, camera_id)
            if path is not None:
                cache[camera_id] = (path, time.monotonic())
            stop_event.wait(interval)  # interruptible sleep

    threads = []
    for camera_id, rtsp_url in cameras:
        t = threading.Thread(
            target=_grabber,
            args=(camera_id, rtsp_url),
            daemon=True,
            name=f"grabber-{_safe_camera_id(camera_id)}",
        )
        t.start()
        threads.append(t)
        logger.debug(f"[Grabber] Started background frame grabber for {camera_id}")
    return threads


def _compose_ambient_frame(
    nature_frame: Optional[np.ndarray],
    camera_frames: list[tuple[str, np.ndarray]],
    screen_w: int,
    screen_h: int,
    server_label: str = "",
) -> np.ndarray:
    """
    Compose a fullscreen ambient display frame.

    Layout:
      - Cameras fill the entire screen in a square-ish grid.
      - Nature feed rendered as a PIP window in the bottom-right corner
        (1/4 screen width, 16:9 aspect ratio).
      - If no cameras are available, nature fills the entire screen.
    """
    base = np.zeros((screen_h, screen_w, 3), dtype=np.uint8)

    if camera_frames:
        # ── Camera grid: fill the entire screen ──
        n = len(camera_frames)
        cols = max(1, int(n ** 0.5 + 0.9999))  # ceil(sqrt(n))
        rows = max(1, (n + cols - 1) // cols)
        tile_w = screen_w // cols
        tile_h = screen_h // rows

        for idx, (cam_id, cam_frame) in enumerate(camera_frames):
            row = idx // cols
            col = idx % cols
            x0 = col * tile_w
            y0 = row * tile_h
            x1 = x0 + tile_w if col < cols - 1 else screen_w
            y1 = y0 + tile_h if row < rows - 1 else screen_h
            tw, th = x1 - x0, y1 - y0
            if tw <= 0 or th <= 0:
                continue
            tile = cv2.resize(cam_frame, (tw, th))
            cv2.rectangle(tile, (0, 0), (tw - 1, th - 1), (50, 50, 50), 1)
            label = cam_id if len(cam_id) <= 18 else cam_id[-18:]
            font_scale = max(0.3, th / 240.0)
            cv2.putText(
                tile, label, (4, th - 6),
                cv2.FONT_HERSHEY_SIMPLEX, font_scale,
                (220, 220, 220), 1, cv2.LINE_AA,
            )
            base[y0:y1, x0:x1] = tile

        # ── Nature PIP: bottom-right corner, 1/4 screen width, 16:9 ──
        if nature_frame is not None:
            pip_w = max(160, screen_w // 4)
            pip_h = (pip_w * 9) // 16
            pip_x = screen_w - pip_w - 8
            pip_y = screen_h - pip_h - 8
            pip = cv2.resize(nature_frame, (pip_w, pip_h))
            cv2.rectangle(pip, (0, 0), (pip_w - 1, pip_h - 1), (200, 200, 200), 2)
            base[pip_y:pip_y + pip_h, pip_x:pip_x + pip_w] = pip

    else:
        # ── No cameras: nature fills the entire screen ──
        if nature_frame is not None:
            base = cv2.resize(nature_frame, (screen_w, screen_h))

    # CamStack server IP label — top-left corner
    if server_label:
        lbl_scale = max(0.4, screen_h / 1600.0)
        lbl_thickness = 1
        (lbl_w, lbl_h), baseline = cv2.getTextSize(
            server_label, cv2.FONT_HERSHEY_SIMPLEX, lbl_scale, lbl_thickness
        )
        pad = 6
        # Semi-transparent dark backing rectangle
        overlay_roi = base[pad : pad + lbl_h + baseline + pad * 2,
                           pad : pad + lbl_w + pad * 2]
        dark = overlay_roi.copy()
        cv2.rectangle(dark, (0, 0), (dark.shape[1] - 1, dark.shape[0] - 1), (0, 0, 0), -1)
        cv2.addWeighted(dark, 0.55, overlay_roi, 0.45, 0, overlay_roi)
        base[pad : pad + lbl_h + baseline + pad * 2,
             pad : pad + lbl_w + pad * 2] = overlay_roi
        cv2.putText(
            base, server_label,
            (pad * 2, pad + lbl_h + pad // 2),
            cv2.FONT_HERSHEY_SIMPLEX, lbl_scale,
            (220, 220, 220), lbl_thickness, cv2.LINE_AA,
        )

    return base


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
    sensitivity = motion_config.get("sensitivity", 8.0)
    frame_threshold = motion_config.get("frame_threshold", 2)
    diff_threshold = motion_config.get("diff_threshold", 8)
    rotation_interval = motion_config.get("rotation_interval", 20)
    idle_fps = motion_config.get("idle_fps", 10.0)
    active_fps = motion_config.get("active_fps", 25.0)
    ring_seconds = motion_config.get("ring_seconds", 180.0)
    k_enter = motion_config.get("k_enter", 5)
    k_disarm = motion_config.get("k_disarm", 2)
    window_size = motion_config.get("window_size", 8)
    cooldown_seconds = motion_config.get("cooldown_seconds", 15.0)
    clip_playback_speed = motion_config.get("clip_playback_speed", 2.0)
    cameras = motion_config.get("cameras", {})
    
    if not cameras:
        logger.error("No cameras configured for motion detection")
        return launch_rtsp_with_watchdog()  # Fall back to single camera mode
    
    # Initialize motion detector (thin façade over CameraStream objects)
    detector = MotionDetector(
        snapshot_interval=snapshot_interval,
        sensitivity=sensitivity,
        frame_threshold=frame_threshold,
        diff_threshold=diff_threshold,
        idle_fps=idle_fps,
        active_fps=active_fps,
        ring_seconds=ring_seconds,
        k_enter=k_enter,
        k_disarm=k_disarm,
        window_size=window_size,
        cooldown_seconds=cooldown_seconds,
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
    # Lookup dict for fast url resolution during on_confirmed callback
    _camera_url_map: dict[str, str] = {cam_id: url for cam_id, url in enabled_cameras}

    # State tracking
    current_camera_idx = 0
    current_camera_id, current_rtsp_url = enabled_cameras[current_camera_idx]
    last_rotation = time.monotonic()
    last_motion_camera = None
    motion_mode = False
    display_interval = max(0.15, min(1.0, snapshot_interval))
    last_display_update = 0.0
    # Ambient nature feed renders independently at ~30fps, decoupled from the
    # camera snapshot_interval which only needs to be as fast as ffmpeg grabs.
    _ambient_interval = 1.0 / 30.0
    last_ambient_update = 0.0
    last_frame_path: Optional[Path] = None
    last_successful_frame_at = time.monotonic()
    startup_time = time.monotonic()
    # Allow this many seconds for RTSP connections and NatureGrabber to produce
    # their first frames before any offline checks are permitted to fire.
    _STARTUP_GRACE = 20.0
    frame_fail_counts: dict[str, int] = {cam_id: 0 for cam_id, _ in enabled_cameras}
    all_offline_fail_threshold = 3
    offline_frame_timeout = max(12.0, (rotation_interval * len(enabled_cameras)) + 3.0)

    try:
        display = StillFrameDisplay()
    except Exception as e:
        logger.warning(f"Still-frame display unavailable, falling back to mpv switching: {e}")
        display = None
    display_capable = display is not None

    _server_ip = get_first_ipv4()
    _server_label = f"CamStack v{VERSION}  \u2022  https://{_server_ip}/"

    # Ambient nature-feed: nature fills the screen when no camera has active motion.
    ambient_nature_feed: bool = motion_config.get("ambient_nature_feed", True)
    nature_grabber: Optional[NatureGrabber] = None
    if ambient_nature_feed and display_capable:
        nature_grabber = NatureGrabber()
        nature_grabber.start()
        logger.info("[AmbientMode] Nature grabber started \u2014 nature feed active in idle mode")

    # Per-camera snapshot path cache for the still-frame renderer.
    # Populated inline in the display loop from CameraStream's latest frame (no
    # separate ffmpeg grabber threads — CameraStream handles persistent RTSP).
    # Entries: None | (Path, grab_timestamp: float)
    _frame_cache: dict[str, tuple[Path, float] | None] = {
        cam_id: None for cam_id, _ in enabled_cameras
    }

    # Wire the on_confirmed callback only when motion recording is enabled.
    # The ambient display runs regardless; this flag only gates clip recording.
    motion_recording_enabled: bool = motion_config.get("enabled", False)

    def _on_motion_confirmed(camera_id: str, pre_frames) -> None:  # type: ignore[type-arg]
        if not motion_recording_enabled:
            return
        cam_url = _camera_url_map.get(camera_id)
        if cam_url:
            cam_score = (
                detector.get_camera_states()
                .get(camera_id, {})
                .get("motion_score", 0.0)
            ) / 100.0
            motion_memory.record_clip(camera_id, cam_url, cam_score, pre_frames=pre_frames)

    detector.on_confirmed = _on_motion_confirmed

    # Start all CameraStream threads (persistent RTSP + K-of-N state machines).
    detector.start_monitoring()

    # Safe defaults so the finally block never hits UnboundLocalError.
    # Only reassigned when display is None (legacy mpv path).
    procs: list = []
    primary = None
    files: list = []

    # Fallback path: if tkinter unavailable use mpv per-camera (legacy mode).
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
            
            # Check for motion (returns camera_id if any stream is in RECORDING state)
            motion_camera_id = detector.check_all_cameras()
            # Note: clip recording is handled by detector.on_confirmed callback which
            # fires once per event with the pre-event ring buffer for context.

            # If all monitored cameras have failed/been disabled, stop showing
            # stale camera imagery and switch to nature fallback behavior.
            camera_states = detector.get_camera_states()
            startup_done = (now - startup_time) >= _STARTUP_GRACE
            # A camera is considered offline when it has been unreachable for
            # _OFFLINE_FAILURE_THRESHOLD consecutive attempts (matching the
            # threshold in camera_stream.py that also clears the display frame).
            _STREAM_OFFLINE_THRESHOLD = 12
            all_cameras_offline = bool(camera_states) and all(
                (not st.get("enabled", True))
                or (st.get("consecutive_failures", 0) >= _STREAM_OFFLINE_THRESHOLD)
                for st in camera_states.values()
            )
            # In ambient mode the nature feed fills the screen; camera tiles are
            # overlays only.  A camera having no frames just means its Q4 tile is
            # absent — that is NOT an all-offline condition.  Only check snapshot
            # failures when there is no nature grabber (single-camera / mpv mode).
            all_snapshots_failing = (
                startup_done
                and nature_grabber is None
                and bool(frame_fail_counts)
                and all(count >= all_offline_fail_threshold for count in frame_fail_counts.values())
            )
            # Frame-timeout check also skipped in ambient mode — nature stream
            # keeps the display alive even when cameras are temporarily offline.
            frame_timeout_exceeded = (
                startup_done
                and nature_grabber is None
                and (now - last_successful_frame_at) >= offline_frame_timeout
            )

            if all_cameras_offline or all_snapshots_failing or frame_timeout_exceeded:
                logger.warning("All motion cameras appear offline; switching to fallback stream")
                detector.stop_monitoring()
                if display is not None:
                    display.show_black()  # black frame so desktop never flashes
                    display.close()
                    display = None
                else:
                    _terminate_procs(procs)
                    _close_files(files)
                camera_urls = [url for _, url in enabled_cameras]
                return _fallback_loop(recover_urls=camera_urls)

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
            
            # Rotation logic (only when no motion AND ambient mode is off)
            if not motion_mode and nature_grabber is None and (now - last_rotation) >= rotation_interval:
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

                # --- Motion Memory: show last clip as rapid still frames ---
                # Window stays open throughout; desktop never exposed.
                if display is not None:
                    mem_entry = motion_memory.get_last_motion(current_camera_id)
                    if mem_entry:
                        clip_path = Path(mem_entry["clip_path"])
                        ago_text = motion_memory.time_since_motion(current_camera_id) or ""
                        annotation = f"{current_camera_id}  \u2022  Last motion: {ago_text}"
                        logger.info(
                            f"[MotionMemory] Showing clip as stills for {current_camera_id} "
                            f"({ago_text}): {clip_path.name}"
                        )
                        _play_clip_as_stills(
                            clip_path,
                            display,
                            annotation=annotation,
                            fps=15,
                            speed=clip_playback_speed,
                            abort_check=lambda: bool(detector.check_all_cameras()),
                        )
                        # Re-read motion state after clip (abort_check may have fired)
                        motion_camera_id = detector.check_all_cameras()

            # Render still frames when GUI mode is available.
            # Ambient nature feed uses its own fast interval; single-camera mode
            # uses the slower display_interval tied to the snapshot rate.
            if display is not None and nature_grabber is not None and not motion_mode:
                if (now - last_ambient_update) >= _ambient_interval:
                    # ── Ambient mode: nature background + all camera tiles in Q4 ──
                    n_frame = nature_grabber.latest_frame
                    cam_tiles: list[tuple[str, np.ndarray]] = []
                    for cam_id, _ in enabled_cameras:
                        f = detector.get_display_frame(cam_id)
                        if f is not None:
                            cam_tiles.append((cam_id, f))
                            frame_fail_counts[cam_id] = 0
                        else:
                            frame_fail_counts[cam_id] = frame_fail_counts.get(cam_id, 0) + 1
                    if n_frame is not None or cam_tiles:
                        last_successful_frame_at = now
                    composite = _compose_ambient_frame(
                        n_frame, cam_tiles, display._width, display._height,
                        server_label=_server_label,
                    )
                    display.show_np_frame(composite)
                    last_ambient_update = now
            elif display is not None and (now - last_display_update) >= display_interval:
                # ── Single-camera mode (motion detected or ambient disabled) ──
                bgr_frame = detector.get_display_frame(current_camera_id)
                if bgr_frame is not None:
                    snap_path = SNAP_DIR / f"live_{_safe_camera_id(current_camera_id)}.jpg"
                    rgb = bgr_frame[:, :, ::-1]
                    Image.fromarray(rgb).save(str(snap_path), "JPEG", quality=85)
                    _frame_cache[current_camera_id] = (snap_path, now)
                cached = _frame_cache.get(current_camera_id)
                max_stale = display_interval * 4 + 2.0
                frame_fresh = (
                    cached is not None
                    and (now - cached[1]) < max_stale
                )
                if frame_fresh:
                    frame_path, _ = cached
                    frame_fail_counts[current_camera_id] = 0
                    last_successful_frame_at = now
                    ago = motion_memory.time_since_motion(current_camera_id)
                    ann_parts = [current_camera_id]
                    if ago:
                        ann_parts.append(f"Last motion: {ago}")
                    ann_parts.append(_server_label)
                    ann_path = SNAP_DIR / f"annotated_{_safe_camera_id(current_camera_id)}.jpg"
                    frame_path = _annotate_frame(frame_path, "  \u2022  ".join(ann_parts), ann_path)
                    if display.show_image(frame_path):
                        last_frame_path = frame_path
                else:
                    frame_fail_counts[current_camera_id] = (
                        frame_fail_counts.get(current_camera_id, 0) + 1
                    )
                    if last_frame_path is not None:
                        ago = motion_memory.time_since_motion(current_camera_id)
                        stale_parts = [current_camera_id]
                        if ago:
                            stale_parts.append(f"Last motion: {ago}")
                        stale_parts.append(_server_label)
                        ann_path = SNAP_DIR / f"annotated_{_safe_camera_id(current_camera_id)}.jpg"
                        show_path = _annotate_frame(
                            last_frame_path, "  \u2022  ".join(stale_parts), ann_path
                        )
                        display.show_image(show_path)
                last_display_update = now

            time.sleep(0.016)  # ~60Hz tick — fine-grained timing for 30fps ambient renders
            
    except KeyboardInterrupt:
        logger.info("Motion detection interrupted by user")
    except Exception as e:
        logger.exception(f"Motion detection error: {e}")
    finally:
        detector.stop_monitoring()    # stops all CameraStream threads
        if nature_grabber is not None:
            nature_grabber.stop()
        if display is not None:
            display.close()
        elif procs:
            _terminate_procs(procs)
            _close_files(files)

    return 0
