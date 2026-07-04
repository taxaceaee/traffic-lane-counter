"""YOLO detector wrapper with ModelRegistry singleton.

The torch + ultralytics imports are done lazily inside ``__init__`` so that
``import tf_worker.pipeline`` and CLI tools stay cheap (don't require the
ML stack).  P1.6 — eager heavy imports.

ModelRegistry
-------------
When multiple cameras use the same model weights, the ModelRegistry ensures
they share ONE model instance in GPU memory.  Without this, each camera
loads its own copy → GPU OOM at N × model_size.

Usage:
    detector = ModelRegistry.get("yolo11s.pt", half=True)
    adapter = YoloByteTrackAdapter(detector, config)
"""
from __future__ import annotations

import threading
import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("trafficflow.model_registry")


class ModelRegistry:
    """Thread-safe singleton registry — one model per (weights, half) combo.

    All cameras with the same ``weights`` + ``half`` setting share a single
    GPU-resident model.  Cache is never evicted (models are small, reloads
    are slow).
    """

    _models: dict[str, Any] = {}
    _lock = threading.Lock()

    @classmethod
    def get(cls, weights_path: str | Path, half: bool = False) -> Any:
        key = f"{weights_path}:half={half}"
        if key not in cls._models:
            with cls._lock:
                if key not in cls._models:  # double-checked locking
                    cls._models[key] = YoloDetectorWrapper(weights_path, half=half)
        return cls._models[key]

    @classmethod
    def preload(cls, models_yaml: str | Path = "configs/models.yaml") -> list[str]:
        """Preload models listed in the ``preload`` section of models.yaml.

        Reads ``configs/models.yaml``, looks for the ``preload`` key, and
        eagerly loads each listed model into GPU memory so that switching
        to them later is instant — no weight-loading latency.

        Safe to call multiple times; models already cached are skipped.
        """
        path = Path(models_yaml)
        if not path.exists():
            logger.warning("models.yaml not found at %s, skipping preload", path)
            return []
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        preload_cfg = raw.get("preload", {})
        if not preload_cfg.get("enabled", False):
            logger.info("Model preload disabled in config")
            return []

        model_ids: list[str] = preload_cfg.get("models", [])
        if not model_ids:
            logger.info("No models listed for preload")
            return []

        # Build a lookup: model_id -> path
        id_to_path: dict[str, str] = {}
        for entry in raw.get("models", []):
            mid = entry.get("model_id")
            mpath = entry.get("path")
            if mid and mpath:
                id_to_path[mid] = mpath

        loaded: list[str] = []
        for mid in model_ids:
            weights = id_to_path.get(mid)
            if not weights:
                logger.warning("Preload: model_id '%s' not found in registry, skipping", mid)
                continue
            try:
                cls.get(weights, half=True)
                loaded.append(mid)
                logger.info("Preloaded model: %s (%s)", mid, weights)
            except Exception as exc:
                logger.error("Failed to preload model '%s' (%s): %s", mid, weights, exc)

        if loaded:
            logger.info("Preloaded %d model(s): %s", len(loaded), loaded)
        return loaded

    @classmethod
    def clear(cls) -> None:
        """Release all models from GPU memory (call during graceful shutdown)."""
        with cls._lock:
            cls._models.clear()


class YoloDetectorWrapper:
    """Wrapper around the Ultralytics YOLO model class for initialization."""

    def __init__(self, weights_path: str | Path, half: bool = False):
        """Initialize YOLO detector.

        Args:
            weights_path: Path to the YOLO weights file (e.g. .pt or .onnx).
            half: Enable FP16 half-precision inference if CUDA is available.
        """
        # Lazy import — keeps ``import tf_worker.pipeline`` cheap for
        # tests, CLI tools, and code paths that never touch the model.
        import torch
        from ultralytics import YOLO

        self.weights_path = str(weights_path)
        self.model = YOLO(self.weights_path)
        # Enable FP16 inference when a CUDA device is available and caller opts in.
        # Half-precision roughly doubles throughput on most NVIDIA GPUs with minimal
        # accuracy loss on vehicle detection tasks.
        self.half = half and torch.cuda.is_available()
        if self.half:
            self.model.model.half()
