import json
import logging
from pathlib import Path
from typing import Any

import yaml

from tf_worker.evaluation.metrics import compute_runtime_metrics, evaluate_sequence
from tf_worker.pipeline import TrafficFlowPipeline

logger = logging.getLogger("trafficflow.benchmark")


def _load_detector(config_path: Path) -> Any:
    """Load detector once and cache it for reuse across sequences."""
    from tf_core.detection.yolo_detector import YoloDetectorWrapper
    from tf_core.io.config_loader import load_and_validate_config

    cfg = load_and_validate_config(config_path)
    detector_cfg = cfg.get("detector", {})
    weights = detector_cfg.get("weights", "weights/yolo11n.pt")
    half = detector_cfg.get("half", False)
    return YoloDetectorWrapper(weights, half=half)


class BenchmarkRunner:
    """Orchestrates running multiple traffic analysis sequences and calculating metrics."""
    def __init__(self, manifest: dict[str, Any], output_dir: str | Path):
        self.manifest = manifest
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> list[dict[str, Any]]:
        """Runs all sequences configured in the manifest.

        Returns:
            A list of result dictionaries containing sequence details and metrics.
        """
        results = []
        sequences = self.manifest.get("sequences", [])

        # Pre-load detector from the first sequence's lane config, then reuse
        detector = None
        if sequences:
            first_cfg = Path(sequences[0]["lane_config"])
            try:
                detector = _load_detector(first_cfg)
            except Exception as exc:
                logger.warning("Could not pre-load detector: %s — falling back to per-sequence load", exc)

        for seq in sequences:
            seq_id = seq["sequence_id"]
            condition = seq.get("condition", "unknown")
            source_path = Path(seq["source"])
            lane_config_path = Path(seq["lane_config"])
            gt_xml_path = Path(seq["gt_xml"]) if "gt_xml" in seq and seq["gt_xml"] is not None else None

            logger.info("\n=========================================")
            logger.info(f"Running Benchmark Sequence: {seq_id} ({condition})")
            logger.info("=========================================")

            # Destination output folder for the sequence
            seq_output_dir = self.output_dir / seq_id
            seq_output_dir.mkdir(parents=True, exist_ok=True)

            # 1. Execute the pipeline (reuse pre-loaded detector if available)
            pipeline = TrafficFlowPipeline(
                config_path=lane_config_path,
                output_dir=seq_output_dir,
                source=source_path,
                detector=detector,
            )

            try:
                run_res = pipeline.run()
            except (OSError, ValueError, RuntimeError):
                logger.exception("Sequence %s failed during execution", seq_id)
                raise

            # 2. Load the lane config dict for evaluation mapping
            with open(lane_config_path, encoding="utf-8") as f:
                config_dict = yaml.safe_load(f)

            # 3. Calculate runtime metrics
            latencies = run_res["total_latencies"]
            timing_records = run_res["timing_records"]
            runtime_metrics = compute_runtime_metrics(latencies, timing_records)

            # 4. Calculate supervised metrics
            frames_jsonl_path = seq_output_dir / "frames.jsonl"
            supervised_metrics = None
            if gt_xml_path is not None:
                try:
                    supervised_metrics = evaluate_sequence(
                        frames_jsonl_path=frames_jsonl_path,
                        gt_xml_path=gt_xml_path,
                        config_dict=config_dict
                    )
                except Exception as e:
                    logger.warning(f"Supervised evaluation failed for {seq_id}: {e}")

            # 5. Build combined metrics dictionary
            combined_metrics = {
                "fps_avg": runtime_metrics["fps_avg"],
                "latency_avg_ms": runtime_metrics["latency_avg_ms"],
                "latency_p50_ms": runtime_metrics["latency_p50_ms"],
                "latency_p95_ms": runtime_metrics["latency_p95_ms"],
                "per_step_ms_avg": runtime_metrics["per_step_ms_avg"],
                "supervised": supervised_metrics
            }

            # 6. Save metrics.json in the sequence's output directory
            metrics_json_path = seq_output_dir / "metrics.json"
            with open(metrics_json_path, "w", encoding="utf-8") as f:
                json.dump(combined_metrics, f, indent=2)

            results.append({
                "sequence_id": seq_id,
                "condition": condition,
                "metrics": combined_metrics,
                "output_dir": str(seq_output_dir)
            })

            logger.info(f"Sequence {seq_id} finished. Average FPS: {combined_metrics['fps_avg']:.2f}")
            if supervised_metrics:
                f1 = supervised_metrics["detection"]["f1"]
                overall_acc = supervised_metrics["occupancy"]["overall_accuracy"]
                logger.info(f"Supervised Results -> Detection F1: {f1:.3f}, Occupancy Accuracy: {overall_acc:.3f}")

        return results
