import json
from pathlib import Path
from typing import Any

import numpy as np

from backend.evaluation.bbox_matching import match_boxes
from backend.evaluation.detrac_xml_parser import parse_detrac_xml
from shared.lanes.lane_assigner import LaneAssigner


def compute_runtime_metrics(latencies: list[float], timing_records: dict[str, list[float]]) -> dict[str, Any]:
    """Computes runtime performance metrics from list of frame latencies and per-step records.

    Args:
        latencies: List of total processing latencies per frame (in milliseconds).
        timing_records: Dict mapping step names to list of step latencies (in milliseconds).

    Returns:
        A dictionary with runtime performance metrics.
    """
    if not latencies:
        return {
            "fps_avg": 0.0,
            "latency_avg_ms": 0.0,
            "latency_p50_ms": 0.0,
            "latency_p95_ms": 0.0,
            "per_step_ms_avg": {step: 0.0 for step in timing_records} if timing_records else {}
        }

    avg_latency = float(np.mean(latencies))
    p50_latency = float(np.median(latencies))
    p95_latency = float(np.percentile(latencies, 95))
    fps = 1000.0 / avg_latency if avg_latency > 0 else 0.0

    per_step = {}
    if timing_records:
        for step, times in timing_records.items():
            per_step[step] = float(np.mean(times)) if times else 0.0

    return {
        "fps_avg": round(fps, 2),
        "latency_avg_ms": round(avg_latency, 2),
        "latency_p50_ms": round(p50_latency, 2),
        "latency_p95_ms": round(p95_latency, 2),
        "per_step_ms_avg": {k: round(v, 2) for k, v in per_step.items()}
    }

def evaluate_sequence(
    frames_jsonl_path: str | Path,
    gt_xml_path: str | Path | None,
    config_dict: dict[str, Any]
) -> dict[str, Any] | None:
    """Evaluates a completed sequence execution against ground truth annotations.

    Args:
        frames_jsonl_path: Path to the generated frames.jsonl log.
        gt_xml_path: Optional path to the UA-DETRAC XML annotation file.
        config_dict: The lane configuration dictionary (used to instantiate LaneAssigner).

    Returns:
        A dictionary of supervised evaluation metrics, or None if gt_xml_path is None.
    """
    if gt_xml_path is None:
        return None

    gt_xml_path = Path(gt_xml_path)
    if not gt_xml_path.exists():
        return None

    # 1. Parse ground truth XML
    gt_frames = parse_detrac_xml(gt_xml_path)

    # 2. Parse predicted frames from JSONL
    pred_frames = {}
    frames_jsonl_path = Path(frames_jsonl_path)
    if not frames_jsonl_path.exists():
        raise FileNotFoundError(f"Frames JSONL log not found at: {frames_jsonl_path}")

    with open(frames_jsonl_path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            data = json.loads(line)
            frame_id = data["frame_id"]
            pred_frames[frame_id] = data

    # 3. Instantiate LaneAssigner to map GT bboxes
    assigner = LaneAssigner(config_dict)
    lane_ids = [lane["id"] for lane in config_dict.get("lanes", [])]

    # Initialize metric accumulators
    total_tp = 0
    total_fp = 0
    total_fn = 0

    total_matched_tp_objects = 0
    correct_lane_assignments = 0

    # Tracking ID switch estimator state
    last_matched_pred_id = {}  # gt_target_id -> pred_track_id
    id_switches = 0

    # Lane occupancy errors accumulator: lane_id -> List of (pred_count, gt_count) per frame
    occupancy_history = {lid: [] for lid in lane_ids}

    # Determine the frames to evaluate (union of frames in GT and predictions)
    all_frame_ids = sorted(list(set(gt_frames.keys()).union(pred_frames.keys())))

    for frame_id in all_frame_ids:
        gt_targets = gt_frames.get(frame_id, [])
        pred_data = pred_frames.get(frame_id, {})

        pred_dets = pred_data.get("detections", [])
        pred_tracks = pred_data.get("tracks", [])
        pred_occ = pred_data.get("occupancy", {})

        # A. Detection Matching (vehicle-level class-agnostic at IoU 0.5 threshold)
        matches_det, unmatched_preds_det, unmatched_gts_det = match_boxes(
            pred_dets, gt_targets, iou_threshold=0.5
        )
        total_tp += len(matches_det)
        total_fp += len(unmatched_preds_det)
        total_fn += len(unmatched_gts_det)

        # B. Tracking ID Switches and Lane Assignment Accuracy
        # We match prediction tracks to GT targets to compute track-based metrics
        matches_trk, _, _ = match_boxes(
            pred_tracks, gt_targets, iou_threshold=0.5
        )

        for p_idx, g_idx in matches_trk:
            gt_obj = gt_targets[g_idx]
            pred_obj = pred_tracks[p_idx]

            gt_tid = gt_obj["target_id"]
            pred_tid = pred_obj["track_id"]

            # ID switch detection
            if gt_tid in last_matched_pred_id and last_matched_pred_id[gt_tid] != pred_tid:
                id_switches += 1
            last_matched_pred_id[gt_tid] = pred_tid

            # Lane Assignment Match checking
            pred_lane = assigner.assign_lane(pred_obj["bbox"])
            gt_lane = assigner.assign_lane(gt_obj["bbox"])

            if pred_lane == gt_lane:
                correct_lane_assignments += 1
            total_matched_tp_objects += 1

        # C. Generate Ground Truth Occupancy
        gt_occ = {lid: 0 for lid in lane_ids}
        for gt_obj in gt_targets:
            lane_id = assigner.assign_lane(gt_obj["bbox"])
            if lane_id in gt_occ:
                gt_occ[lane_id] += 1

        # Record occupancy differences for this frame
        for lid in lane_ids:
            p_val = pred_occ.get(lid, 0)
            g_val = gt_occ[lid]
            occupancy_history[lid].append((p_val, g_val))

    # Calculate aggregate detection metrics
    precision = total_tp / (total_tp + total_fp) if (total_tp + total_fp) > 0 else 0.0
    recall = total_tp / (total_tp + total_fn) if (total_tp + total_fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    # Calculate lane assignment accuracy
    lane_assign_accuracy = (
        correct_lane_assignments / total_matched_tp_objects if total_matched_tp_objects > 0 else 1.0
    )

    # Calculate occupancy metrics per lane
    lane_metrics = {}
    overall_acc_sum = 0.0

    for lid in lane_ids:
        history = occupancy_history[lid]
        if not history:
            lane_metrics[lid] = {
                "mae": 0.0,
                "rmse": 0.0,
                "overcount_frames": 0,
                "undercount_frames": 0,
                "accuracy": 1.0
            }
            overall_acc_sum += 1.0
            continue

        p_vals = np.array([h[0] for h in history])
        g_vals = np.array([h[1] for h in history])

        errors = p_vals - g_vals
        abs_errors = np.abs(errors)
        sq_errors = errors ** 2

        mae = float(np.mean(abs_errors))
        rmse = float(np.sqrt(np.mean(sq_errors)))

        overcounts = int(np.sum(errors > 0))
        undercounts = int(np.sum(errors < 0))
        exact_matches = int(np.sum(errors == 0))

        accuracy = exact_matches / len(history)
        overall_acc_sum += accuracy

        lane_metrics[lid] = {
            "mae": round(mae, 3),
            "rmse": round(rmse, 3),
            "overcount_frames": overcounts,
            "undercount_frames": undercounts,
            "accuracy": round(accuracy, 3)
        }

    overall_accuracy = overall_acc_sum / len(lane_ids) if lane_ids else 1.0

    return {
        "detection": {
            "tp": total_tp,
            "fp": total_fp,
            "fn": total_fn,
            "precision": round(precision, 3),
            "recall": round(recall, 3),
            "f1": round(f1, 3)
        },
        "tracking": {
            "id_switches": id_switches
        },
        "lane_assignment": {
            "accuracy": round(lane_assign_accuracy, 3)
        },
        "occupancy": {
            "overall_accuracy": round(overall_accuracy, 3),
            "lanes": lane_metrics
        }
    }
