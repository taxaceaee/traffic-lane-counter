#!/usr/bin/env python3
"""CLI wrapper for running a single TrafficFlow pipeline job."""

from __future__ import annotations

import argparse
from pathlib import Path

from tf_worker.pipeline import TrafficFlowPipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a TrafficFlow pipeline on one source.")
    parser.add_argument("--source", required=True, help="Video path, image directory, or URL")
    parser.add_argument("--config", required=True, help="Compiled pipeline YAML config")
    parser.add_argument("--output-dir", required=True, help="Directory for outputs")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    pipeline = TrafficFlowPipeline(
        config_path=Path(args.config),
        output_dir=Path(args.output_dir),
        source=args.source,
    )
    try:
        pipeline.run()
    finally:
        pipeline.stop()


if __name__ == "__main__":
    main()
