"""
camera_stream.py — persistent RTSP reader with ring buffer and K-of-N motion state machine.

Each ``CameraStream`` owns one daemon thread that:
  - Maintains a persistent ``cv2.VideoCapture`` to the camera's RTSP URL (auto-reconnects on error)
  - Decodes every frame, resizes to ANALYSIS_W × ANALYSIS_H (small copy for motion analysis)
  - Appends the small copy to a :class:`collections.deque` ring buffer capped at *ring_seconds* worth of
    frames (pre-event buffer available when motion fires)
  - Stores the full-resolution BGR frame as ``latest_display_frame`` for the player to show
  - Runs K-of-N motion scoring to drive a four-state machine:
      IDLE → RECORDING (fires ``on_confirmed`` callback) → COOLDOWN → IDLE

The K-of-N approach: a sliding window of *window_size* motion-score booleans is kept.
  - Arm when ``sum(window) >= k_enter``
  - Disarm (stay armed / go to cooldown) when ``sum(window) < k_disarm``

This replaces the old cold-start-ffmpeg-per-frame approach used by both the background frame
grabbers in ``player.py`` and the snapshot-diff loop in ``motion_detector.py``.
"""
from __future__ import annotations

import enum
import threading
import time
from collections import deque
from typing import Callable, Deque, Optional

import cv2
import numpy as np
from loguru import logger

# Resolution of frames stored in the ring buffer and used for motion analysis.
# Smaller = less RAM and faster diff computation. Display frames are stored full-size.
ANALYSIS_W = 320
ANALYSIS_H = 240

# Interval (seconds) between reconnect attempts after a capture failure.
RECONNECT_DELAY = 5.0


class CamStreamState(enum.Enum):
    IDLE = "IDLE"
    RECORDING = "RECORDING"
    COOLDOWN = "COOLDOWN"


class CameraStream:
    """
    Persistent RTSP reader + ring buffer + K-of-N motion state machine.

    Parameters
    ----------
    camera_id:
        Human-readable identifier (e.g. ``"cam1"``).
    rtsp_url:
        Full RTSP URL including credentials.
    idle_fps:
        Target frame rate while in IDLE / COOLDOWN states.
    active_fps:
        Target frame rate while in RECORDING state.
    ring_seconds:
        Duration of the pre-event ring buffer.  At *idle_fps* fps the buffer
        holds ``ring_seconds * idle_fps`` analysis frames (~900 at 5 fps × 180 s).
    sensitivity:
        Percentage of pixels that must change (0–100) before a frame counts as
        containing motion.
    diff_threshold:
        Per-pixel absolute-difference threshold (0–255) for the grayscale delta.
    k_enter:
        Number of motion-positive frames in the sliding window required to arm.
    k_disarm:
        Drop below this count in the sliding window to transition to COOLDOWN.
    window_size:
        Number of frames in the K-of-N sliding window.
    cooldown_seconds:
        Seconds to stay in COOLDOWN before returning to IDLE.
    on_confirmed:
        ``Callable[[str, deque[np.ndarray]], None]`` invoked *exactly once* per
        motion event with ``(camera_id, pre_frames_snapshot)``.  It is called
        from the stream's daemon thread; implementations should be non-blocking
        (e.g. hand work off to another thread).
    """

    def __init__(
        self,
        camera_id: str,
        rtsp_url: str,
        *,
        idle_fps: float = 5.0,
        active_fps: float = 15.0,
        ring_seconds: float = 180.0,
        sensitivity: float = 8.0,
        diff_threshold: int = 8,
        k_enter: int = 5,
        k_disarm: int = 2,
        window_size: int = 8,
        cooldown_seconds: float = 15.0,
        on_confirmed: Optional[Callable[[str, "Deque[np.ndarray]"], None]] = None,
    ) -> None:
        self.camera_id = camera_id
        self.rtsp_url = rtsp_url
        self.idle_fps = max(1.0, idle_fps)
        self.active_fps = max(1.0, active_fps)
        self.sensitivity = float(sensitivity)
        self.diff_threshold = int(diff_threshold)
        self.k_enter = k_enter
        self.k_disarm = k_disarm
        self.window_size = window_size
        self.cooldown_seconds = cooldown_seconds
        self.on_confirmed = on_confirmed

        # Ring buffer: store analysis-sized frames for pre-event context.
        ring_maxlen = max(10, int(ring_seconds * self.active_fps))
        self._ring: Deque[np.ndarray] = deque(maxlen=ring_maxlen)

        # Latest full-resolution frame for the display renderer.
        self._latest_display_frame: Optional[np.ndarray] = None
        self._display_lock = threading.Lock()

        # State machine
        self._state = CamStreamState.IDLE
        self._state_lock = threading.Lock()
        self._cooldown_end: float = 0.0

        # K-of-N motion window
        self._motion_window: Deque[bool] = deque(maxlen=window_size)

        # Public motion score (0.0–1.0) — updated each frame, readable from outside
        self._last_motion_score: float = 0.0

        # Threading
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"camstream-{camera_id}",
            daemon=True,
        )
        self._enabled = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background capture + analysis thread."""
        self._thread.start()
        logger.info(f"[{self.camera_id}] CameraStream started")

    def stop(self) -> None:
        """Signal the background thread to stop and wait for it to exit."""
        self._stop_event.set()
        self._thread.join(timeout=8.0)
        logger.info(f"[{self.camera_id}] CameraStream stopped")

    @property
    def state(self) -> CamStreamState:
        with self._state_lock:
            return self._state

    @property
    def latest_display_frame(self) -> Optional[np.ndarray]:
        """The most recent full-resolution BGR frame; ``None`` if none decoded yet."""
        with self._display_lock:
            return self._latest_display_frame

    @property
    def motion_score(self) -> float:
        """Last per-frame motion score as a fraction 0.0–1.0."""
        return self._last_motion_score

    @property
    def ring_buffer(self) -> "Deque[np.ndarray]":
        """Read-only view of the analysis frame ring buffer."""
        return self._ring

    def get_state_dict(self) -> dict:
        """Return a serialisable snapshot of this stream's state (for backwards-compat)."""
        return {
            "state": self._state.value,
            "motion_score": round(self._last_motion_score * 100.0, 2),
            "enabled": self._enabled,
            "k_window_sum": int(sum(self._motion_window)),
        }

    def set_sensitivity(self, sensitivity: float) -> None:
        self.sensitivity = float(sensitivity)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _open_capture(self) -> Optional[cv2.VideoCapture]:
        """Open (or reopen) the RTSP capture with TCP transport."""
        url = self.rtsp_url
        # Prefer TCP transport for reliability on Wi-Fi / NAT cameras.
        if "rtsp_transport" not in url.lower():
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        else:
            cap = cv2.VideoCapture(url)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        # Reduce ffmpeg open/read timeouts from the 30s default to 8s so that
        # a dead camera reconnect cycle takes ~10s instead of ~35s.
        try:
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 8_000)
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 8_000)
        except Exception:
            pass  # property may not exist on older OpenCV builds
        if not cap.isOpened():
            logger.warning(f"[{self.camera_id}] Failed to open RTSP capture: {url}")
            return None
        logger.info(f"[{self.camera_id}] RTSP capture opened: {url}")
        return cap

    def _transition(self, new_state: CamStreamState) -> None:
        with self._state_lock:
            old = self._state
            self._state = new_state
        if old != new_state:
            logger.info(f"[{self.camera_id}] State: {old.value} → {new_state.value}")

    def _score_frame(
        self, gray: np.ndarray, prev_gray: Optional[np.ndarray]
    ) -> float:
        """Return fraction of pixels changed (0.0–1.0) vs *prev_gray*."""
        if prev_gray is None or gray.shape != prev_gray.shape:
            return 0.0
        delta = cv2.absdiff(gray, prev_gray)
        changed = int(np.count_nonzero(delta > self.diff_threshold))
        total = gray.size
        return changed / total if total > 0 else 0.0

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        cap: Optional[cv2.VideoCapture] = None
        prev_gray: Optional[np.ndarray] = None
        consecutive_failures = 0

        while not self._stop_event.is_set():
            # Open / reopen capture as needed.
            if cap is None or not cap.isOpened():
                if cap is not None:
                    cap.release()
                    cap = None
                    prev_gray = None
                cap = self._open_capture()
                if cap is None:
                    consecutive_failures += 1
                    logger.warning(
                        f"[{self.camera_id}] Reconnect attempt {consecutive_failures}; "
                        f"retrying in {RECONNECT_DELAY}s"
                    )
                    self._stop_event.wait(RECONNECT_DELAY)
                    continue
                consecutive_failures = 0

            frame_start = time.monotonic()

            ret, frame = cap.read()
            if not ret or frame is None:
                logger.debug(f"[{self.camera_id}] Frame read failed; reopening capture")
                cap.release()
                cap = None
                prev_gray = None
                self._stop_event.wait(1.0)
                continue

            # Store full-resolution frame for display.
            with self._display_lock:
                self._latest_display_frame = frame

            # Resize to analysis resolution.
            small = cv2.resize(frame, (ANALYSIS_W, ANALYSIS_H), interpolation=cv2.INTER_AREA)

            # Append to ring buffer regardless of motion state.
            self._ring.append(small)

            # Only run motion analysis if enabled.
            if self._enabled:
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                score = self._score_frame(gray, prev_gray)
                self._last_motion_score = score
                prev_gray = gray

                is_motion = score >= (self.sensitivity / 100.0)
                self._motion_window.append(is_motion)
                window_sum = sum(self._motion_window)

                current_state = self.state

                if current_state == CamStreamState.IDLE:
                    if (
                        len(self._motion_window) == self.window_size
                        and window_sum >= self.k_enter
                    ):
                        # Snapshot the pre-event ring buffer NOW before RECORDING adds more frames.
                        pre_frames: Deque[np.ndarray] = deque(self._ring)
                        self._transition(CamStreamState.RECORDING)
                        # Fire the callback (non-blocking — caller must not block here).
                        if self.on_confirmed is not None:
                            try:
                                self.on_confirmed(self.camera_id, pre_frames)
                            except Exception:
                                logger.exception(
                                    f"[{self.camera_id}] on_confirmed callback raised"
                                )

                elif current_state == CamStreamState.RECORDING:
                    if window_sum < self.k_disarm:
                        self._cooldown_end = time.monotonic() + self.cooldown_seconds
                        self._transition(CamStreamState.COOLDOWN)

                elif current_state == CamStreamState.COOLDOWN:
                    if time.monotonic() >= self._cooldown_end:
                        self._motion_window.clear()
                        self._transition(CamStreamState.IDLE)

            # Throttle to the appropriate FPS.
            current_state = self.state
            target_fps = (
                self.active_fps
                if current_state == CamStreamState.RECORDING
                else self.idle_fps
            )
            elapsed = time.monotonic() - frame_start
            sleep_time = max(0.0, (1.0 / target_fps) - elapsed)
            if sleep_time > 0:
                self._stop_event.wait(sleep_time)

        if cap is not None:
            cap.release()
        logger.info(f"[{self.camera_id}] Capture thread exiting")
