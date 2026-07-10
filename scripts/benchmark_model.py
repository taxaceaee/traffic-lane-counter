#!/usr/bin/env python3
"""Benchmark YOLO model inference FPS at various imgsz x half configs.

Usage:
    python -m scripts.benchmark_model [--model weights/yolo11n.pt] [--warmup 50] [--iters 200]

Tests all 4 combinations: imgsz=960/half=False, imgsz=960/half=True,
imgsz=640/half=False, imgsz=640/half=True.

Uses torch.cuda.synchronize() for accurate GPU timing.
Prints FPS and ms/frame for each config.
"""
import argparse
import time
from pathlib import Path

import numpy as np
import torch


def benchmark(
    model_path: str,
    imgsz: int,
    half: bool,
    warmup: int,
    iters: int,
) -> float:
    from ultralytics import YOLO

    model = YOLO(model_path)
    device = "cuda" if torch.cuda.is_available() else "cpu"

    if half and torch.cuda.is_available():
        model.model.half()
        dtype = "float16"
    else:
        dtype = "float32"

    # Dummy BGR frame matching typical camera input
    dummy = np.random.randint(0, 255, (imgsz, imgsz, 3), dtype=np.uint8)

    # Warmup — includes model graph compilation on first call
    for _ in range(warmup):
        model.predict(dummy, imgsz=imgsz, verbose=False)

    if device == "cuda":
        torch.cuda.synchronize()

    # Measurement
    start = time.perf_counter()
    for _ in range(iters):
        model.predict(dummy, imgsz=imgsz, verbose=False)
    if device == "cuda":
        torch.cuda.synchronize()
    elapsed = time.perf_counter() - start

    fps = iters / elapsed
    ms_per_frame = (elapsed / iters) * 1000.0
    return fps, ms_per_frame, dtype


def main():
    parser = argparse.ArgumentParser(description="Benchmark YOLO inference FPS")
    parser.add_argument(
        "--model",
        default="weights/yolo11n.pt",
        help="Path to YOLO weights (default: weights/yolo11n.pt)",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=50,
        help="Warmup iterations before measurement (default: 50)",
    )
    parser.add_argument(
        "--iters",
        type=int,
        default=200,
        help="Iterations for measurement (default: 200)",
    )
    args = parser.parse_args()

    model_path = Path(args.model)
    if not model_path.exists():
        print(f"ERROR: Model not found: {model_path}")
        print("Download with: yolo export model=yolo11n.pt || yolo predict model=yolo11n.pt")
        return

    has_cuda = torch.cuda.is_available()
    gpu_name = torch.cuda.get_device_name(0) if has_cuda else "N/A (CPU)"
    print(f"Device: {gpu_name}")
    print(f"CUDA available: {has_cuda}")
    print(f"Warmup: {args.warmup} iters, Measure: {args.iters} iters")
    print()

    configs = [
        (960, False),
        (960, True),
        (640, False),
        (640, True),
    ]

    results = []
    for imgsz, half in configs:
        if half and not has_cuda:
            print(f"  imgsz={imgsz} half=True → SKIP (no CUDA)")
            continue

        fps, ms, dtype = benchmark(
            str(model_path), imgsz=imgsz, half=half,
            warmup=args.warmup, iters=args.iters,
        )
        results.append((imgsz, dtype, fps, ms))
        print(f"  imgsz={imgsz} {dtype:>8}: {fps:>7.1f} FPS  ({ms:>6.2f} ms/frame)")

    print()
    if results:
        print("Summary:")
        for imgsz, dtype, fps, ms in results:
            print(f"  imgsz={imgsz} {dtype:>8}: {fps:>7.1f} FPS  ({ms:>6.2f} ms/frame)")


if __name__ == "__main__":
    main()
