#!/usr/bin/env python3
"""CLI wrapper for the benchmark runner."""

from __future__ import annotations

import argparse
from pathlib import Path

from tf_worker.benchmark.manifest import load_manifest
from tf_worker.benchmark.report_writer import write_summary_report
from tf_worker.benchmark.runner import BenchmarkRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the TrafficFlow benchmark suite.")
    parser.add_argument(
        "--manifest",
        default="configs/detrac_benchmark_manifest.yaml",
        help="Benchmark manifest YAML",
    )
    parser.add_argument(
        "--output-dir",
        default="outputs/benchmark",
        help="Directory for benchmark artifacts",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    manifest = load_manifest(args.manifest)
    output_dir = Path(args.output_dir)
    runner = BenchmarkRunner(manifest=manifest, output_dir=output_dir)
    results = runner.run()
    write_summary_report(results, output_dir)


if __name__ == "__main__":
    main()
