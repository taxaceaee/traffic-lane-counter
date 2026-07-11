"""Structured logging setup for TrafficFlow.

Replaces basicConfig with json-formatted logs when LOG_FORMAT=json.
Fields: timestamp, level, name, message, and extra context (camera_id,
frame_idx, job_id, latency_ms).

Usage:
    from tf_common.log_setup import setup_logging
    setup_logging()
"""
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any


class StructuredFormatter(logging.Formatter):
    """JSON log formatter with optional extra fields."""

    def format(self, record: logging.LogRecord) -> str:
        extra = {}
        for key in ("camera_id", "job_id", "frame_idx", "latency_ms",
                     "event_count", "queue_depth", "track_id", "lane_id"):
            val = getattr(record, key, None)
            if val is not None:
                extra[key] = val

        log_entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = self.formatException(record.exc_info)
        if extra:
            log_entry["extra"] = extra

        return json.dumps(log_entry, default=str)


def setup_logging() -> None:
    log_format = os.getenv("LOG_FORMAT", "text").lower()
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()

    handler = logging.StreamHandler(sys.stdout)
    if log_format == "json":
        handler.setFormatter(StructuredFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s — %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))

    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level, logging.INFO))
    # Remove default handlers, add ours
    for h in root.handlers[:]:
        root.removeHandler(h)
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Get a logger with structured extra field support."""
    return logging.getLogger(name)
