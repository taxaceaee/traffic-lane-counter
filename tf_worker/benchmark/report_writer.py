import csv
import logging
from pathlib import Path
from typing import Any


def write_summary_report(results: list[dict[str, Any]], output_dir: str | Path):
    """Writes summary reports (CSV and Markdown) for the benchmark execution.

    Args:
        results: List of result dicts returned by BenchmarkRunner.run().
        output_dir: Path to the directory where reports will be saved.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Write summary_metrics.csv
    csv_path = output_dir / "summary_metrics.csv"
    headers = [
        "sequence_id",
        "condition",
        "fps_avg",
        "latency_avg_ms",
        "detection_precision",
        "detection_recall",
        "detection_f1",
        "id_switches",
        "lane_assignment_accuracy",
        "occupancy_accuracy"
    ]

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)

        for res in results:
            seq_id = res["sequence_id"]
            cond = res["condition"]
            metrics = res["metrics"]
            supervised = metrics.get("supervised")

            row = [
                seq_id,
                cond,
                metrics["fps_avg"],
                metrics["latency_avg_ms"]
            ]

            if supervised is not None:
                det = supervised.get("detection", {})
                row.extend([
                    det.get("precision", "N/A"),
                    det.get("recall", "N/A"),
                    det.get("f1", "N/A"),
                    supervised.get("tracking", {}).get("id_switches", "N/A"),
                    supervised.get("lane_assignment", {}).get("accuracy", "N/A"),
                    supervised.get("occupancy", {}).get("overall_accuracy", "N/A")
                ])
            else:
                row.extend(["N/A"] * 6)

            writer.writerow(row)

    # 2. Write baseline_report.md
    md_path = output_dir / "baseline_report.md"

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# UA-DETRAC Baseline Performance Report\n\n")
        f.write("This report summarizes the baseline performance of the traffic analysis pipeline ")
        f.write("using pretrained YOLO detection + ByteTrack across the benchmark sequences.\n\n")

        # Markdown summary table
        f.write("## Overview Metrics Summary\n\n")
        f.write("| Sequence | Condition | FPS | Latency (ms) | Precision | Recall | F1 | ID Switches | Lane Assign Acc | Occ Acc |\n")
        f.write("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n")

        for res in results:
            seq_id = res["sequence_id"]
            cond = res["condition"]
            metrics = res["metrics"]
            supervised = metrics.get("supervised")

            fps = f"{metrics['fps_avg']:.1f}"
            lat = f"{metrics['latency_avg_ms']:.1f}"

            if supervised is not None:
                det = supervised.get("detection", {})
                prec = f"{det.get('precision', 0.0):.3f}"
                rec = f"{det.get('recall', 0.0):.3f}"
                f1 = f"{det.get('f1', 0.0):.3f}"
                id_sw = str(supervised.get("tracking", {}).get("id_switches", 0))
                la_acc = f"{supervised.get('lane_assignment', {}).get('accuracy', 0.0):.3f}"
                occ_acc = f"{supervised.get('occupancy', {}).get('overall_accuracy', 0.0):.3f}"
            else:
                prec, rec, f1, id_sw, la_acc, occ_acc = ["N/A"] * 6

            f.write(f"| {seq_id} | {cond} | {fps} | {lat} | {prec} | {rec} | {f1} | {id_sw} | {la_acc} | {occ_acc} |\n")

        f.write("\n## Runtime Performance Breakdown\n\n")
        f.write("Average execution times per step across all processed frames (in milliseconds):\n\n")
        f.write("| Sequence | Read | Detect/Track | Lane Assign | Occupancy | Write | Visualize |\n")
        f.write("| --- | --- | --- | --- | --- | --- | --- |\n")

        for res in results:
            seq_id = res["sequence_id"]
            metrics = res["metrics"]
            per_step = metrics.get("per_step_ms_avg", {})

            r = f"{per_step.get('read', 0.0):.2f}"
            dt = f"{per_step.get('detect_track', 0.0):.2f}"
            la = f"{per_step.get('lane_assign', 0.0):.2f}"
            occ = f"{per_step.get('occupancy', 0.0):.2f}"
            w = f"{per_step.get('write', 0.0):.2f}"
            v = f"{per_step.get('visualize', 0.0):.2f}"

            f.write(f"| {seq_id} | {r} | {dt} | {la} | {occ} | {w} | {v} |\n")

        f.write("\n## Detailed Lane Occupancy Analysis\n\n")
        for res in results:
            seq_id = res["sequence_id"]
            supervised = res["metrics"].get("supervised")
            if supervised is None:
                continue

            f.write(f"### Sequence {seq_id} Lane Metrics\n\n")
            f.write("| Lane ID | MAE | RMSE | Overcount Frames | Undercount Frames | Frame-level Accuracy |\n")
            f.write("| --- | --- | --- | --- | --- | --- |\n")

            lanes = supervised.get("occupancy", {}).get("lanes", {})
            for lane_id, lm in lanes.items():
                mae = f"{lm.get('mae', 0.0):.3f}"
                rmse = f"{lm.get('rmse', 0.0):.3f}"
                oc = lm.get("overcount_frames", 0)
                uc = lm.get("undercount_frames", 0)
                acc = f"{lm.get('accuracy', 0.0):.3f}"
                f.write(f"| {lane_id} | {mae} | {rmse} | {oc} | {uc} | {acc} |\n")
            f.write("\n")

    logging.getLogger("trafficflow.benchmark").info(f"Summary metrics exported to {csv_path}")
    logging.getLogger("trafficflow.benchmark").info(f"Markdown report generated at {md_path}")
