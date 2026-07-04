"""YOLO detector wrapper.

The torch + ultralytics imports are done lazily inside ``__init__`` so that
``import backend.pipeline`` and CLI tools stay cheap (don't require the
ML stack).  P1.6 — eager heavy imports.
"""
from __future__ import annotations

from pathlib import Path


class YoloDetectorWrapper:
    """Wrapper around the Ultralytics YOLO model class for initialization."""

    def __init__(self, weights_path: str | Path, half: bool = False):
        """Initialize YOLO detector.

        Args:
            weights_path: Path to the YOLO weights file (e.g. .pt or .onnx).
            half: Enable FP16 half-precision inference if CUDA is available.
        """
        # Lazy import — keeps ``import backend.pipeline`` cheap for
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
