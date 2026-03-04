"""
motion_detector.py — thin façade over ``CameraStream`` objects.

Public API is preserved exactly so ``player.py`` requires only minimal changes:

  - ``MotionDetector(...)``          constructor
  - ``detector.add_camera(...)``     register a camera
  - ``detector.check_all_cameras()`` returns camera_id in RECORDING state, or None
  - ``detector.get_camera_states()`` dict[camera_id, state_dict]
  - ``detector.start_monitoring()``  start all streams
  - ``detector.stop_monitoring()``   stop all streams
  - ``detector.on_confirmed``        callable set by player.py to receive motion events

The old cold-start-ffmpeg-per-frame approach is completely replaced by persistent
``cv2.VideoCapture`` connections managed inside each ``CameraStream``.
"""
from __future__ import annotations

from collections import deque
from typing import Callable, Deque, Optional

import numpy as np
from loguru import logger

from .camera_stream import CameraStream, CamStreamState


class MotionDetector:
    """
    Façade that owns a dict of ``CameraStream`` objects, one per camera.

    Parameters
    ----------
    snapshot_interval:
        Ignored (kept for API compatibility).  Frame rate is now governed
        by ``idle_fps`` / ``active_fps`` inside each ``CameraStream``.
    sensitivity:
        Percentage of pixels that must change before a frame is considered
        to contain motion (passed to each ``CameraStream``).
    frame_threshold:
        Ignored (K-of-N window replaces the old consecutive-frame counter).
    diff_threshold:
        Per-pixel grayscale difference threshold (passed to each ``CameraStream``).
    idle_fps:
        Capture rate while no motion is detected.
    active_fps:
        Capture rate while recording an event.
    ring_seconds:
        Length of the pre-event ring buffer in seconds.
    k_enter:
        Minimum positive frames in the K-of-N window to arm motion.
    k_disarm:
        Drop below this to transition to COOLDOWN.
    window_size:
        Sliding window size for K-of-N scoring.
    cooldown_seconds:
        Seconds to wait in COOLDOWN before re-arming.
    """

    def __init__(
        self,
        snapshot_interval: float = 1.0,   # kept for API compatibility
        sensitivity: float = 8.0,
        frame_threshold: int = 2,          # kept for API compatibility
        diff_threshold: int = 8,
        idle_fps: float = 5.0,
        active_fps: float = 15.0,
        ring_seconds: float = 180.0,
        k_enter: int = 5,
        k_disarm: int = 2,
        window_size: int = 8,
        cooldown_seconds: float = 15.0,
    ) -> None:
        self._sensitivity = sensitivity
        self._diff_threshold = diff_threshold
        self._idle_fps = idle_fps
        self._active_fps = active_fps
        self._ring_seconds = ring_seconds
        self._k_enter = k_enter
        self._k_disarm = k_disarm
        self._window_size = window_size
        self._cooldown_seconds = cooldown_seconds

        self._streams: dict[str, CameraStream] = {}

        # Set this callable from player.py to receive motion-confirmed events.
        # Signature: (camera_id: str, pre_frames: deque[np.ndarray]) -> None
        self.on_confirmed: Optional[Callable[[str, Deque[np.ndarray]], None]] = None

    # ------------------------------------------------------------------
    # Camera management
    # ------------------------------------------------------------------

    def add_camera(
        self,
        camera_id: str,
        rtsp_url: str,
        enabled: bool = True,
    ) -> None:
        """Register a camera; creates its ``CameraStream`` (not yet started)."""
        if camera_id in self._streams:
            logger.warning(f"[MotionDetector] Camera {camera_id!r} already registered; ignoring")
            return

        stream = CameraStream(
            camera_id=camera_id,
            rtsp_url=rtsp_url,
            idle_fps=self._idle_fps,
            active_fps=self._active_fps,
            ring_seconds=self._ring_seconds,
            sensitivity=self._sensitivity,
            diff_threshold=self._diff_threshold,
            k_enter=self._k_enter,
            k_disarm=self._k_disarm,
            window_size=self._window_size,
            cooldown_seconds=self._cooldown_seconds,
            on_confirmed=self._on_stream_confirmed,
        )
        stream.set_enabled(enabled)
        self._streams[camera_id] = stream
        logger.info(f"[MotionDetector] Registered camera {camera_id!r}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start_monitoring(self) -> None:
        """Start all registered ``CameraStream`` threads."""
        for stream in self._streams.values():
            stream.start()
        logger.info(f"[MotionDetector] Started {len(self._streams)} camera stream(s)")

    def stop_monitoring(self) -> None:
        """Stop all ``CameraStream`` threads (blocks until each exits or times out)."""
        for stream in self._streams.values():
            stream.stop()
        logger.info("[MotionDetector] All camera streams stopped")

    # ------------------------------------------------------------------
    # Query API (called from the player main loop)
    # ------------------------------------------------------------------

    def check_all_cameras(self) -> Optional[str]:
        """
        Return the camera_id of the first stream currently in RECORDING state,
        or ``None`` if no camera is active.

        This is the "is there live motion right now?" query used by the player.
        """
        for camera_id, stream in self._streams.items():
            if stream.state == CamStreamState.RECORDING:
                return camera_id
        return None

    def get_camera_states(self) -> dict[str, dict]:
        """
        Return a dict camera_id → state_dict for all registered cameras.

        The state_dict schema is compatible with what ``player.py`` previously
        expected from the old ``MotionDetector``::

            {
                "state":        "IDLE" | "RECORDING" | "COOLDOWN",
                "motion_score": float,   # 0–100 percent
                "enabled":      bool,
                "k_window_sum": int,
            }
        """
        return {cid: stream.get_state_dict() for cid, stream in self._streams.items()}

    def get_display_frame(self, camera_id: str) -> "Optional[np.ndarray]":
        """
        Return the latest full-resolution BGR frame for *camera_id*, or ``None``.

        This replaces the old background ffmpeg frame-grabber cache in ``player.py``.
        """
        stream = self._streams.get(camera_id)
        if stream is None:
            return None
        return stream.latest_display_frame

    # ------------------------------------------------------------------
    # Internal callback (fired from CameraStream daemon thread)
    # ------------------------------------------------------------------

    def _on_stream_confirmed(
        self,
        camera_id: str,
        pre_frames: Deque[np.ndarray],
    ) -> None:
        """Forward to the player's on_confirmed handler (set externally)."""
        logger.info(
            f"[MotionDetector] Motion confirmed on {camera_id!r} "
            f"({len(pre_frames)} pre-event frames)"
        )
        if self.on_confirmed is not None:
            try:
                self.on_confirmed(camera_id, pre_frames)
            except Exception:
                logger.exception(
                    f"[MotionDetector] on_confirmed handler raised for {camera_id!r}"
                )
