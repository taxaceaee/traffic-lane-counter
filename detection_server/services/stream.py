"""ContinuousCameraStream — runs per-camera detection loop in background.

One instance per camera.  Reads frames from a video source (RTSP, file,
YouTube, image_dir), feeds them through DetectionCore, and pushes
structured detection results to a callback.
"""
import logging
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from shared.detection_core import DetectionCore
from shared.roi import CropROI

logger = logging.getLogger("detection_server.stream")


class ContinuousCameraStream:
    """Long-running detection loop for a single camera.

    Parameters
    ----------
    camera_id:
        Unique camera identifier.
    source:
        Video source (RTSP URL, file path, YouTube URL, image directory).
    source_type:
        One of: video, rtsp, youtube, youtube_live, image_dir.
    config:
        Compiled pipeline config dict (model, lanes, thresholds etc.).
    zone_polygons:
        Optional list of detection zone polygons for ROI crop.
    push_callback:
        Callable receiving (camera_id, result_dict) on each frame.
        Called from the capture thread — must be fast/async-safe.
    fps:
        Target capture rate.  If source is slower, actual FPS is lower.
    reconnect:
        Whether to auto-reconnect on source drop (live sources).
    """

    def __init__(
        self,
        camera_id: str,
        source: str,
        source_type: str,
        config: dict[str, Any],
        zone_polygons: list[list[list[float]]] | None = None,
        push_callback: Any | None = None,
        fps: float = 25.0,
        reconnect: bool = True,
    ):
        self.camera_id = camera_id
        self.source = source
        self.source_type = source_type
        self.config = config
        self.zone_polygons = zone_polygons
        self.push_callback = push_callback
        self.target_fps = fps
        self.reconnect = reconnect

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._stats: dict[str, Any] = {
            "camera_id": camera_id,
            "status": "stopped",
            "frame_idx": 0,
            "fps": 0.0,
            "track_count": 0,
            "lane_count": 0,
            "occupancy": {},
            "started_at": None,
            "errors": 0,
        }
        self._stats_lock = threading.Lock()

        self._core: DetectionCore | None = None
        self._cap: cv2.VideoCapture | None = None
        self._image_dir_files: list[str] = []
        self._image_dir_idx: int = 0

        # ── Double-buffering (frame read-ahead) ────────────────────
        # A background thread reads the next frame while the main
        # thread processes the current one.  This overlaps I/O decode
        # latency (typically 5-30 ms for RTSP) with GPU inference.
        self._next_frame: np.ndarray | None = None
        self._next_frame_ready = threading.Event()
        self._reader_thread: threading.Thread | None = None
        self._reader_stop = threading.Event()
        self._reader_error: Exception | None = None

        # ── Config auto-reload ────────────────────────────────────
        # Periodically checks YAML mtime for lane/zone changes.
        # When lanes change, calls DetectionCore.update_lanes() (no reset).
        # When zones change, reloads zone polygons + resets CropROI.
        self._last_lanes_mtime: float = 0.0
        self._last_zones_mtime: float = 0.0
        self._last_config_check: float = 0.0
        self._config_check_interval: float = 5.0  # seconds

    def _check_config_reload(self) -> None:
        """Check if lane/zone config YAML changed on disk and hot-update."""
        now = time.monotonic()
        if now - self._last_config_check < self._config_check_interval:
            return
        self._last_config_check = now

        self._reload_lanes()
        self._reload_zones()

    def _reload_lanes(self) -> None:
        lanes_path = self.config.get("_lanes_path")
        if not lanes_path:
            return
        lanes_path = Path(lanes_path)
        if not lanes_path.exists():
            return
        try:
            mtime = lanes_path.stat().st_mtime
        except OSError:
            return
        if mtime <= self._last_lanes_mtime:
            return
        self._last_lanes_mtime = mtime

        import yaml
        try:
            with open(lanes_path, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except (yaml.YAMLError, OSError) as e:
            logger.warning("Camera %s: failed to reload lanes: %s", self.camera_id, e)
            return

        new_lanes_raw = raw.get("lanes", [])
        if not new_lanes_raw:
            return

        new_lanes = []
        for item in new_lanes_raw:
            entry = {"id": item["lane_id"], "name": item.get("name", ""), "points": item["polygon"]}
            cl = item.get("counting_line")
            if cl:
                entry["counting_line"] = cl
            new_lanes.append(entry)

        try:
            self._core.update_lanes(new_lanes)
            self._stats["lane_count"] = len(new_lanes)
            logger.info("Camera %s: hot-updated %d lanes", self.camera_id, len(new_lanes))
        except Exception:
            logger.exception("Camera %s: lane hot-update failed", self.camera_id)

    def _reload_zones(self) -> None:
        zones_path = self.config.get("_zones_path")
        if not zones_path:
            return
        zones_path = Path(zones_path)
        if not zones_path.exists():
            return
        try:
            mtime = zones_path.stat().st_mtime
        except OSError:
            return
        if mtime <= self._last_zones_mtime:
            return
        self._last_zones_mtime = mtime

        import yaml
        try:
            with open(zones_path, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except (yaml.YAMLError, OSError) as e:
            logger.warning("Camera %s: failed to reload zones: %s", self.camera_id, e)
            return

        new_zones_raw = raw.get("zones", [])
        if not new_zones_raw:
            self.zone_polygons = None
        else:
            self.zone_polygons = [z["polygon"] for z in new_zones_raw]

        # Reset CropROI — will be recreated on next _crop_frame() call
        self._roi = None
        logger.info("Camera %s: hot-updated %d detection zones", self.camera_id,
                     len(self.zone_polygons or []))

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    @property
    def stats(self) -> dict[str, Any]:
        with self._stats_lock:
            return dict(self._stats)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            logger.warning("Camera %s already running", self.camera_id)
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"stream-{self.camera_id}",
            daemon=True,
        )
        self._thread.start()
        with self._stats_lock:
            self._stats["status"] = "starting"
            self._stats["started_at"] = datetime.now(timezone.utc).isoformat()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._cleanup()
        with self._stats_lock:
            self._stats["status"] = "stopped"

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        reconnect_count = 0
        max_reconnect = 10 if self.reconnect else 1

        while not self._stop_event.is_set() and reconnect_count < max_reconnect:
            try:
                self._init_inference()
                with self._stats_lock:
                    self._stats["status"] = "running"
                    self._stats["lane_count"] = len(self._core.lane_assigner.lanes) if self._core and self._core.lane_assigner and hasattr(self._core.lane_assigner, 'lanes') else 0

                self._capture_loop()

            except (cv2.error, OSError, ValueError, RuntimeError) as e:
                logger.error("Pipeline error for camera %s: %s", self.camera_id, e)
                with self._stats_lock:
                    self._stats["errors"] += 1

            finally:
                self._cleanup()

            if self._stop_event.is_set():
                break

            if not self.reconnect:
                break

            reconnect_count += 1
            backoff = min(1.0 * (2 ** (reconnect_count - 1)), 60.0)
            logger.info("Camera %s: reconnecting in %.1fs (attempt %d/%d)",
                        self.camera_id, backoff, reconnect_count, max_reconnect)
            self._stop_event.wait(timeout=backoff)

        with self._stats_lock:
            self._stats["status"] = "stopped"

        logger.info("Camera %s pipeline stopped (frames: %d)",
                     self.camera_id, self._core.frame_idx if self._core else 0)

    def _init_inference(self) -> None:
        if self._core is None:
            self._core = DetectionCore(self.config)
            self._core.start()
            logger.info("DetectionCore initialized for camera %s", self.camera_id)
        else:
            self._core.reset()

        if self.source_type == "image_dir":
            import glob as _glob
            import os as _os
            img_exts = (".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif")
            src_dir = self.source
            self._image_dir_files = sorted([
                f for f in _os.listdir(src_dir)
                if f.lower().endswith(img_exts)
            ])
            self._image_dir_idx = 0
            if not self._image_dir_files:
                raise ValueError(f"No images found in {src_dir}")
            logger.info("Camera %s: image_dir with %d files", self.camera_id, len(self._image_dir_files))
        else:
            # Resolve YouTube URLs to direct stream URLs before passing to OpenCV
            source = self.source
            if self.source_type in ("youtube", "youtube_live"):
                from shared.yt_utils import resolve_stream_url as _resolve_yt_url
                source = _resolve_yt_url(source)
            self._cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG)
            if not self._cap.isOpened():
                raise ValueError(f"Could not open video source: {self.source}")

    def _capture_loop(self) -> None:
        frame_interval = 1.0 / max(self.target_fps, 1.0) if self.target_fps > 0 else 0.04
        t_last_fps_update = time.monotonic()
        frame_count_since_fps = 0

        # Start reader thread for double-buffered frame read-ahead
        self._reader_stop.clear()
        self._next_frame = None
        self._next_frame_ready.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_func, name=f"read-{self.camera_id}", daemon=True
        )
        self._reader_thread.start()

        # Prime the first frame
        self._next_frame_ready.wait(timeout=5.0)
        if self._reader_error:
            raise self._reader_error

        while not self._stop_event.is_set():
            t_start = time.monotonic()

            # Check YAML config for lane changes (non-blocking, async check)
            self._check_config_reload()

            # Grab the pre-read frame (block on first, then immediate)
            frame = self._next_frame
            self._next_frame = None
            self._next_frame_ready.clear()
            # Signal reader to fetch next frame (overlap starts here)
            # The reader is already running in a loop, so it picks up immediately

            if frame is None:
                if self.source_type in ("rtsp", "youtube", "youtube_live"):
                    logger.warning("Camera %s: lost source, attempting reconnect", self.camera_id)
                    raise OSError("Source disconnected")
                break

            if self.zone_polygons:
                frame = self._crop_frame(frame)

            result = self._core.process_frame(frame)

            if self.push_callback:
                try:
                    self.push_callback(self.camera_id, result)
                except Exception:
                    logger.exception("Push callback failed for camera %s", self.camera_id)

            # Wait for reader to have the next frame ready (should already be)
            if self._next_frame is None:
                self._next_frame_ready.wait(timeout=1.0)

            # Update FPS stats
            frame_count_since_fps += 1
            now = time.monotonic()
            if now - t_last_fps_update >= 2.0:
                with self._stats_lock:
                    self._stats["frame_idx"] = result["frame_idx"]
                    self._stats["fps"] = frame_count_since_fps / (now - t_last_fps_update)
                    self._stats["track_count"] = len(result.get("tracks", []))
                    self._stats["occupancy"] = result.get("occupancy", {})
                frame_count_since_fps = 0
                t_last_fps_update = now

            # Frame rate control (only sleep if we are ahead of target)
            elapsed = time.monotonic() - t_start
            sleep_time = frame_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    def _reader_func(self) -> None:
        """Background thread: continuously reads frames into ``self._next_frame``."""
        try:
            while not self._reader_stop.is_set():
                if self._next_frame is not None:
                    # Main thread hasn't consumed yet — wait a bit
                    self._reader_stop.wait(timeout=0.001)
                    continue

                frame = self._read_frame()
                if frame is None:
                    break

                self._next_frame = frame
                self._next_frame_ready.set()
        except Exception as e:
            self._reader_error = e
        finally:
            self._next_frame_ready.set()  # unblock main thread on error

    def _read_frame(self) -> np.ndarray | None:
        if self.source_type == "image_dir":
            if self._image_dir_idx >= len(self._image_dir_files):
                return None
            path = f"{self.source}/{self._image_dir_files[self._image_dir_idx]}"
            self._image_dir_idx += 1
            frame = cv2.imread(path)
            if frame is not None and frame.size > 0:
                return frame
            return None
        else:
            if self._cap is None:
                return None
            ret, frame = self._cap.read()
            if not ret or frame is None or frame.size == 0:
                return None
            return frame

    def _crop_frame(self, frame: np.ndarray) -> np.ndarray:
        if not hasattr(self, '_roi') or self._roi is None:
            self._roi = CropROI([], {
                "width": self.config.get("frame_size", {}).get("width", 960),
                "height": self.config.get("frame_size", {}).get("height", 540),
            }, padding=50, zone_polygons=self.zone_polygons)
        return self._roi.crop(frame)

    def _cleanup(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        self._image_dir_files = []
        self._image_dir_idx = 0

        # Stop reader thread
        self._reader_stop.set()
        self._next_frame_ready.set()
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=3.0)
        self._reader_thread = None
        self._next_frame = None
        self._reader_error = None
