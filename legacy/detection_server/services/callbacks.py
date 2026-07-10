"""callbacks.py — push detection results from continuous streams to external consumers.

Two built-in strategies:
1. HTTP callback — POST results to a backend URL per frame
2. Ring buffer — in-memory buffer, queriable via GET /stream/output/{camera_id}
"""
import json
import threading
from collections import deque
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import URLError
import logging

logger = logging.getLogger("detection_server.callbacks")


class HTTPCallback:
    """Push each detection result to a backend URL via HTTP POST."""

    def __init__(self, endpoint: str, timeout: float = 2.0, max_retries: int = 2):
        self.endpoint = endpoint
        self.timeout = timeout
        self.max_retries = max_retries

    def __call__(self, camera_id: str, result: dict[str, Any]) -> None:
        payload = self._serialize(camera_id, result)
        data = json.dumps(payload).encode("utf-8")

        for attempt in range(self.max_retries + 1):
            try:
                req = Request(self.endpoint, data=data, method="POST")
                req.add_header("Content-Type", "application/json")
                req.add_header("X-Camera-Id", camera_id)
                req.add_header("X-Frame-Idx", str(result.get("frame_idx", 0)))
                urlopen(req, timeout=self.timeout)
                return
            except URLError as e:
                if attempt < self.max_retries:
                    logger.debug("HTTP push for %s attempt %d failed: %s", camera_id, attempt + 1, e)
                else:
                    logger.warning("HTTP push for %s failed after %d retries: %s",
                                   camera_id, self.max_retries + 1, e)

    def _serialize(self, camera_id: str, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "camera_id": camera_id,
            "frame_idx": result["frame_idx"],
            "timestamp": result["frame_timestamp"].isoformat() if hasattr(result["frame_timestamp"], "isoformat") else str(result["frame_timestamp"]),
            "tracks": [
                {
                    "track_id": t["track_id"],
                    "class_name": t["class_name"],
                    "confidence": t["confidence"],
                    "bbox": t["bbox"],
                }
                for t in result.get("tracks", [])
            ],
            "occupancy": result.get("occupancy", {}),
            "crossings": [
                {
                    "frame": c["frame"],
                    "track_id": c["track_id"],
                    "class_name": c["class_name"],
                    "lane_id": c["lane_id"],
                    "line_id": c["line_id"],
                    "direction": c["direction"],
                    "confidence": c["confidence"],
                }
                for c in result.get("crossings", [])
            ],
            "events": result.get("events", []),
            "timing_ms": result.get("timing_ms", {}),
        }


class RingBufferCallback:
    """Buffer the last N detection results in memory (per camera).

    Query via GET /stream/output/{camera_id} for real-time dashboard.
    """

    def __init__(self, maxlen: int = 100):
        self._buffers: dict[str, deque] = {}
        self._lock = threading.Lock()
        self._maxlen = maxlen

    def __call__(self, camera_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            if camera_id not in self._buffers:
                self._buffers[camera_id] = deque(maxlen=self._maxlen)
            self._buffers[camera_id].append(self._serialize(camera_id, result))

    def get_latest(self, camera_id: str, count: int = 10) -> list[dict]:
        with self._lock:
            buf = self._buffers.get(camera_id)
            if not buf:
                return []
            items = list(buf)[-count:]
            return items

    def clear(self, camera_id: str) -> None:
        with self._lock:
            self._buffers.pop(camera_id, None)

    def _serialize(self, camera_id: str, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "camera_id": camera_id,
            "frame_idx": result["frame_idx"],
            "timestamp": result["frame_timestamp"].isoformat() if hasattr(result["frame_timestamp"], "isoformat") else str(result["frame_timestamp"]),
            "tracks": [
                {
                    "track_id": t["track_id"],
                    "class_name": t["class_name"],
                    "confidence": t["confidence"],
                    "bbox": t["bbox"],
                }
                for t in result.get("tracks", [])
            ],
            "occupancy": result.get("occupancy", {}),
            "crossings": [
                {
                    "frame": c["frame"],
                    "track_id": c["track_id"],
                    "class_name": c["class_name"],
                    "lane_id": c["lane_id"],
                    "line_id": c["line_id"],
                    "direction": c["direction"],
                    "confidence": c["confidence"],
                }
                for c in result.get("crossings", [])
            ],
            "timing_ms": result.get("timing_ms", {}),
        }
