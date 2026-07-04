"""StreamManager — manages all per-camera continuous detection streams.

Provides API to start/stop/list streams and exposes real-time stats.
"""
import logging
import threading
from typing import Any

from detection_server.services.stream import ContinuousCameraStream

logger = logging.getLogger("detection_server.manager")


class StreamManager:
    """Central registry for per-camera DetectionCore streams.

    Thread-safe — all public methods acquire a lock.
    """

    def __init__(self):
        self._streams: dict[str, ContinuousCameraStream] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_stream(
        self,
        camera_id: str,
        source: str,
        source_type: str,
        config: dict[str, Any],
        zone_polygons: list[list[list[float]]] | None = None,
        push_callback: Any | None = None,
        fps: float = 25.0,
        reconnect: bool = True,
    ) -> dict[str, Any]:
        """Start (or restart) a continuous detection stream for a camera.

        If a stream with this camera_id already exists, it is stopped
        and replaced.
        """
        with self._lock:
            self._stop_existing(camera_id)

            stream = ContinuousCameraStream(
                camera_id=camera_id,
                source=source,
                source_type=source_type,
                config=config,
                zone_polygons=zone_polygons,
                push_callback=push_callback,
                fps=fps,
                reconnect=reconnect,
            )
            stream.start()
            self._streams[camera_id] = stream

            logger.info("Stream started for camera %s (source=%s type=%s)",
                        camera_id, source[:80], source_type)
            return {"camera_id": camera_id, "status": "started"}

    def stop_stream(self, camera_id: str) -> dict[str, Any]:
        """Stop detection stream for a camera."""
        with self._lock:
            return self._stop_existing(camera_id)

    def get_stats(self, camera_id: str | None = None) -> dict[str, Any]:
        """Get stats for a specific camera or all cameras."""
        with self._lock:
            if camera_id:
                stream = self._streams.get(camera_id)
                if stream is None:
                    return {"error": f"No stream for camera {camera_id}"}
                return stream.stats

            result = {"camera_count": len(self._streams), "streams": {}}
            for cid, stream in self._streams.items():
                result["streams"][cid] = stream.stats
            return result

    def list_streams(self) -> list[str]:
        """Return list of active camera_ids."""
        with self._lock:
            return sorted(self._streams.keys())

    def restart_stream(self, camera_id: str) -> dict[str, Any]:
        """Restart a running stream (e.g., after config change)."""
        with self._lock:
            stream = self._streams.get(camera_id)
            if stream is None:
                return {"error": f"No stream for camera {camera_id}"}

            stream.stop(timeout=5.0)
            stream._config = stream.config  # config might have been updated
            stream.start()
            return {"camera_id": camera_id, "status": "restarted"}

    def stop_all(self, timeout: float = 15.0) -> None:
        """Stop all streams — call on shutdown."""
        with self._lock:
            for cid, stream in list(self._streams.items()):
                try:
                    stream.stop(timeout=timeout)
                except Exception:
                    logger.exception("Error stopping stream for %s", cid)
            self._streams.clear()
            logger.info("All streams stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _stop_existing(self, camera_id: str) -> dict[str, Any]:
        stream = self._streams.pop(camera_id, None)
        if stream is not None:
            stream.stop(timeout=5.0)
            logger.info("Stream stopped for camera %s", camera_id)
            return {"camera_id": camera_id, "status": "stopped"}
        return {"camera_id": camera_id, "status": "not_found"}
