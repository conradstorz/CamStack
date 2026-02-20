"""
Motion detection module for CamStack multi-camera monitoring.

Uses simple frame differencing to detect motion across multiple camera streams.
Designed to be lightweight and extensible for future algorithm upgrades.
"""
from __future__ import annotations
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from loguru import logger
import numpy as np
from PIL import Image

SNAP_DIR = Path("/opt/camstack/runtime/snaps")
SNAP_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class CameraMotionState:
    """Tracks motion detection state for a single camera."""
    camera_id: str
    rtsp_url: str
    enabled: bool = True
    last_frame: Optional[np.ndarray] = None
    motion_frames: int = 0
    motion_score: float = 0.0
    last_check: float = field(default_factory=time.time)
    error_count: int = 0
    

class MotionDetector:
    """Multi-camera motion detection manager."""
    
    def __init__(
        self,
        snapshot_interval: float = 1.0,
        sensitivity: float = 12.0,
        frame_threshold: int = 3,
        diff_threshold: int = 15,
        max_error_count: int = 5,
    ):
        """
        Initialize motion detector.
        
        Args:
            snapshot_interval: Seconds between snapshots (0.5-2.0 recommended)
            sensitivity: Pixel change threshold percentage (1-30)
            frame_threshold: Consecutive frames with motion before triggering
            diff_threshold: Per-pixel grayscale delta (0-255) considered "changed"
            max_error_count: Max errors before disabling a camera
        """
        self.snapshot_interval = snapshot_interval
        self.sensitivity = sensitivity / 100.0  # Convert to decimal
        self.frame_threshold = frame_threshold
        self.diff_threshold = max(1, min(255, int(diff_threshold)))
        self.max_error_count = max_error_count
        
        self.cameras: dict[str, CameraMotionState] = {}
        self._lock = threading.RLock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._latest_best_camera: Optional[str] = None
        self._latest_best_score: float = 0.0
        self._latest_eval_time: float = 0.0
        
    def add_camera(self, camera_id: str, rtsp_url: str, enabled: bool = True):
        """Add a camera to motion monitoring."""
        with self._lock:
            self.cameras[camera_id] = CameraMotionState(
                camera_id=camera_id,
                rtsp_url=rtsp_url,
                enabled=enabled,
            )
            logger.info(f"Added camera {camera_id} to motion monitoring")
    
    def remove_camera(self, camera_id: str):
        """Remove a camera from monitoring."""
        with self._lock:
            if camera_id in self.cameras:
                del self.cameras[camera_id]
                logger.info(f"Removed camera {camera_id} from motion monitoring")
    
    def enable_camera(self, camera_id: str, enabled: bool = True):
        """Enable or disable motion monitoring for a camera."""
        with self._lock:
            if camera_id in self.cameras:
                self.cameras[camera_id].enabled = enabled
                logger.info(f"Camera {camera_id} motion monitoring: {enabled}")
    
    def update_settings(
        self,
        snapshot_interval: Optional[float] = None,
        sensitivity: Optional[float] = None,
        frame_threshold: Optional[int] = None,
    ):
        """Update motion detection settings."""
        if snapshot_interval is not None:
            self.snapshot_interval = snapshot_interval
        if sensitivity is not None:
            self.sensitivity = sensitivity / 100.0
        if frame_threshold is not None:
            self.frame_threshold = frame_threshold
        logger.info(
            f"Motion settings updated: interval={self.snapshot_interval}s, "
            f"sensitivity={self.sensitivity*100}%, threshold={self.frame_threshold}"
        )
    
    def _grab_snapshot(self, rtsp_url: str, camera_id: str) -> Optional[np.ndarray]:
        """Grab a single frame from RTSP stream as numpy array."""
        snap_path = SNAP_DIR / f"motion_{camera_id.replace('.', '_').replace(':', '_')}.jpg"
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-rtsp_transport", "tcp", "-i", rtsp_url,
            "-frames:v", "1", "-s", "320x240", "-q:v", "5", "-y", str(snap_path)
        ]
        try:
            subprocess.run(cmd, check=True, timeout=5, capture_output=True)
            if snap_path.exists() and snap_path.stat().st_size > 0:
                img = Image.open(snap_path).convert('L')  # Grayscale
                return np.array(img, dtype=np.float32)
        except Exception as e:
            logger.debug(f"Snapshot grab failed for {camera_id}: {e}")
        return None
    
    def _detect_motion(self, cam_state: CameraMotionState) -> tuple[bool, float]:
        """
        Detect motion for a single camera using frame differencing.
        
        Returns:
            (motion_detected, motion_score)
        """
        current_frame = self._grab_snapshot(cam_state.rtsp_url, cam_state.camera_id)
        
        if current_frame is None:
            cam_state.error_count += 1
            if cam_state.error_count >= self.max_error_count:
                logger.warning(
                    f"Camera {cam_state.camera_id} exceeded error limit, disabling"
                )
                cam_state.enabled = False
            return False, 0.0
        
        # Reset error count on successful grab
        cam_state.error_count = 0
        
        # First frame - no comparison possible
        if cam_state.last_frame is None:
            cam_state.last_frame = current_frame
            return False, 0.0
        
        # Compute frame difference
        diff = np.abs(current_frame - cam_state.last_frame)
        
        # Calculate percentage of pixels that changed significantly
        changed_pixels = np.sum(diff > self.diff_threshold)
        total_pixels = diff.size
        change_percentage = changed_pixels / total_pixels
        
        # Update state
        cam_state.last_frame = current_frame
        motion_detected = change_percentage > self.sensitivity
        
        if motion_detected:
            cam_state.motion_frames += 1
            cam_state.motion_score = change_percentage
        else:
            cam_state.motion_frames = 0
            cam_state.motion_score = 0.0
        
        return motion_detected, change_percentage
    
    def check_all_cameras(self) -> Optional[str]:
        """
        Check all enabled cameras for motion.
        
        Returns:
            camera_id of camera with highest sustained motion, or None
        """
        with self._lock:
            best_camera = None
            best_score = 0.0
            checked_any = False
            
            for cam_id, cam_state in self.cameras.items():
                if not cam_state.enabled:
                    continue
                
                # Check if enough time has passed since last check
                now = time.time()
                if now - cam_state.last_check < self.snapshot_interval:
                    continue
                
                cam_state.last_check = now
                checked_any = True
                
                # Detect motion
                motion_detected, score = self._detect_motion(cam_state)
                
                # Check if motion threshold met
                if cam_state.motion_frames >= self.frame_threshold:
                    if score > best_score:
                        best_score = score
                        best_camera = cam_id
                        logger.debug(
                            f"Camera {cam_id} motion: {cam_state.motion_frames} frames, "
                            f"score={score*100:.1f}%"
                        )

            # Only update cached decision when a real evaluation happened.
            # If this call lands between intervals, return the previous fresh result
            # instead of forcing a false "no motion" state.
            if checked_any:
                self._latest_best_camera = best_camera
                self._latest_best_score = best_score
                self._latest_eval_time = time.time()

            hold_seconds = max(1.5, self.snapshot_interval * 2.0)
            if self._latest_best_camera and (time.time() - self._latest_eval_time) <= hold_seconds:
                return self._latest_best_camera

            return None
    
    def get_camera_states(self) -> dict[str, dict]:
        """Get current state of all cameras for monitoring/debugging."""
        with self._lock:
            return {
                cam_id: {
                    "enabled": cam.enabled,
                    "motion_frames": cam.motion_frames,
                    "motion_score": round(cam.motion_score * 100, 2),
                    "error_count": cam.error_count,
                }
                for cam_id, cam in self.cameras.items()
            }
    
    def start_monitoring(self):
        """Start background monitoring thread."""
        if self._running:
            logger.warning("Motion detector already running")
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._monitoring_loop, daemon=True)
        self._thread.start()
        logger.info("Motion detection monitoring started")
    
    def stop_monitoring(self):
        """Stop background monitoring thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Motion detection monitoring stopped")
    
    def _monitoring_loop(self):
        """Background thread that continuously checks for motion."""
        while self._running:
            try:
                self.check_all_cameras()
                time.sleep(0.1)  # Small sleep to prevent busy loop
            except Exception as e:
                logger.exception(f"Error in motion monitoring loop: {e}")
                time.sleep(1)
