"""
Motion Memory - NVR-like clip retention for CamStack.

Records a short video clip when motion is confirmed on a camera, retains the
last N clips per camera on disk, and exposes timing information so the player
can display "Last motion: X ago" overlays during quiet periods.

MVP scope:
  - One background recording per camera (no parallel records for same camera)
  - libx264/ultrafast encode; audio stripped (lightweight, Pi-safe)
  - JSON persistence; survives service restarts
  - API integration deferred to next increment

Public surface intentionally minimal so the future API layer can be added
without touching this module.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from loguru import logger

# ---------------------------------------------------------------------------#
# Paths / constants                                                           #
# ---------------------------------------------------------------------------#

CLIPS_DIR = Path("/opt/camstack/runtime/clips")
MEMORY_FILE = Path("/opt/camstack/runtime/motion_memory.json")

DEFAULT_CLIP_DURATION: int = 12     # seconds of video captured per motion event
MAX_CLIPS_PER_CAMERA: int = 3       # oldest clips pruned automatically
RECORD_TIMEOUT: int = 25            # hard subprocess timeout (must exceed clip duration)


# ---------------------------------------------------------------------------#
# Helpers                                                                     #
# ---------------------------------------------------------------------------#

def _safe_id(camera_id: str) -> str:
    """Sanitise camera_id for use in filenames."""
    return "".join(c if c.isalnum() else "_" for c in camera_id)


def format_motion_age(timestamp: float) -> str:
    """
    Return a human-readable 'how long ago' string from a Unix timestamp.

    Examples: 'just now', '8s ago', '2m 34s ago', '1h 5m ago'
    """
    elapsed = max(0.0, time.time() - timestamp)
    if elapsed < 5:
        return "just now"
    if elapsed < 60:
        return f"{int(elapsed)}s ago"
    if elapsed < 3600:
        m = int(elapsed // 60)
        s = int(elapsed % 60)
        return f"{m}m {s}s ago"
    h = int(elapsed // 3600)
    m = int((elapsed % 3600) // 60)
    return f"{h}h {m}m ago"


# ---------------------------------------------------------------------------#
# MotionMemory                                                               #
# ---------------------------------------------------------------------------#

class MotionMemory:
    """
    Manages last-motion clip retention per camera.

    Thread-safe.  All recording happens in daemon threads so the caller's
    main loop is never blocked.
    """

    def __init__(self, clip_duration: int = DEFAULT_CLIP_DURATION) -> None:
        CLIPS_DIR.mkdir(parents=True, exist_ok=True)
        self.clip_duration = clip_duration
        self._lock = threading.Lock()
        self._recording: set[str] = set()          # camera IDs in-flight
        self._data: dict[str, dict] = {}           # camera_id → entry
        self._load()

    # ------------------------------------------------------------------ #
    # Persistence                                                          #
    # ------------------------------------------------------------------ #

    def _load(self) -> None:
        """Load persisted memory; silently drop entries whose clip file is missing."""
        try:
            if MEMORY_FILE.exists():
                raw = json.loads(MEMORY_FILE.read_text())
                for cam_id, entry in raw.items():
                    cp = entry.get("clip_path")
                    if cp and Path(cp).exists():
                        self._data[cam_id] = entry
                        logger.debug(
                            f"[MotionMemory] Restored entry for {cam_id}: "
                            f"{Path(cp).name}"
                        )
        except Exception as e:
            logger.warning(f"[MotionMemory] Could not load persisted state: {e}")

    def _save(self) -> None:
        """Persist current in-memory state to JSON (called under self._lock)."""
        try:
            MEMORY_FILE.write_text(json.dumps(self._data, indent=2))
        except Exception as e:
            logger.warning(f"[MotionMemory] Could not persist state: {e}")

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def record_clip(
        self,
        camera_id: str,
        rtsp_url: str,
        score: float = 0.0,
    ) -> None:
        """
        Trigger a background clip recording for *camera_id*.

        No-op if a recording is already in progress for the same camera.
        Non-blocking — returns immediately.
        """
        with self._lock:
            if camera_id in self._recording:
                return
            self._recording.add(camera_id)

        threading.Thread(
            target=self._do_record,
            args=(camera_id, rtsp_url, score),
            daemon=True,
            name=f"motmem-rec-{_safe_id(camera_id)}",
        ).start()

    def get_last_motion(self, camera_id: str) -> Optional[dict]:
        """
        Return the most recent motion entry for *camera_id*, or ``None``.

        Entry shape::

            {
                "clip_path": "/opt/camstack/runtime/clips/192_168_1_100_1708723800.mp4",
                "timestamp": 1708723800,
                "score": 0.0312
            }
        """
        with self._lock:
            entry = self._data.get(camera_id)
            if entry:
                cp = entry.get("clip_path")
                if cp and Path(cp).exists():
                    return dict(entry)
                # Clip was pruned / deleted externally — drop stale entry
                del self._data[camera_id]
                self._save()
        return None

    def time_since_motion(self, camera_id: str) -> Optional[str]:
        """
        Return a human-readable 'X ago' string for *camera_id*,
        or ``None`` if no motion has been recorded for this camera yet.
        """
        entry = self.get_last_motion(camera_id)
        if not entry:
            return None
        return format_motion_age(entry["timestamp"])

    def is_recording(self, camera_id: str) -> bool:
        """True while a background clip recording is in progress."""
        with self._lock:
            return camera_id in self._recording

    def all_entries(self) -> dict[str, dict]:
        """
        Return a shallow copy of all in-memory entries (for future API use).
        """
        with self._lock:
            return {
                cam_id: dict(entry)
                for cam_id, entry in self._data.items()
            }

    # ------------------------------------------------------------------ #
    # Internal                                                             #
    # ------------------------------------------------------------------ #

    def _do_record(self, camera_id: str, rtsp_url: str, score: float) -> None:
        """Background worker: capture clip via ffmpeg and update state."""
        safe = _safe_id(camera_id)
        ts = int(time.time())
        clip_path = CLIPS_DIR / f"{safe}_{ts}.mp4"

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-i", rtsp_url,
            "-t", str(self.clip_duration),
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-an",       # no audio (Pi CPU budget)
            "-y", str(clip_path),
        ]

        try:
            logger.info(
                f"[MotionMemory] Recording motion clip: camera={camera_id} "
                f"file={clip_path.name} score={score:.3f}"
            )
            subprocess.run(cmd, timeout=RECORD_TIMEOUT, check=True, capture_output=True)

            if clip_path.exists() and clip_path.stat().st_size > 0:
                with self._lock:
                    self._data[camera_id] = {
                        "clip_path": str(clip_path),
                        "timestamp": ts,
                        "score": round(score, 4),
                    }
                    self._save()
                self._prune_old_clips(safe)
                logger.info(f"[MotionMemory] Clip saved → {clip_path.name}")
            else:
                logger.warning(
                    f"[MotionMemory] Clip missing or empty for {camera_id}; "
                    "possibly RTSP stream rejected connection"
                )
                clip_path.unlink(missing_ok=True)

        except subprocess.TimeoutExpired:
            logger.warning(
                f"[MotionMemory] Recording timed out for {camera_id} "
                f"(>{RECORD_TIMEOUT}s)"
            )
            clip_path.unlink(missing_ok=True)

        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode(errors="replace").strip()
            logger.warning(
                f"[MotionMemory] ffmpeg failed for {camera_id}: {stderr[:200]}"
            )
            clip_path.unlink(missing_ok=True)

        except Exception as e:
            logger.warning(f"[MotionMemory] Unexpected error for {camera_id}: {e}")
            clip_path.unlink(missing_ok=True)

        finally:
            with self._lock:
                self._recording.discard(camera_id)

    def _prune_old_clips(self, safe_id: str) -> None:
        """Keep only the newest MAX_CLIPS_PER_CAMERA clips for this camera."""
        clips = sorted(
            CLIPS_DIR.glob(f"{safe_id}_*.mp4"),
            key=lambda p: p.stat().st_mtime,
        )
        for old in clips[:-MAX_CLIPS_PER_CAMERA]:
            try:
                old.unlink()
                logger.debug(f"[MotionMemory] Pruned old clip: {old.name}")
            except Exception:
                pass
