from typing import Any


def compute_iou(box1: list[float], box2: list[float]) -> float:
    """Computes Intersection over Union (IoU) of two bounding boxes.

    Boxes are in [xmin, ymin, xmax, ymax] format.
    """
    x1 = max(box1[0], box2[0])
    y1 = max(box1[1], box2[1])
    x2 = min(box1[2], box2[2])
    y2 = min(box1[3], box2[3])

    if x2 <= x1 or y2 <= y1:
        return 0.0

    intersection = (x2 - x1) * (y2 - y1)
    area1 = (box1[2] - box1[0]) * (box1[3] - box1[1])
    area2 = (box2[2] - box2[0]) * (box2[3] - box2[1])
    union = area1 + area2 - intersection

    if union <= 0.0:
        return 0.0
    return intersection / union

def match_boxes(
    pred_boxes: list[dict[str, Any]],
    gt_boxes: list[dict[str, Any]],
    iou_threshold: float = 0.5
) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """Matches predicted boxes to ground truth boxes using greedy matching.

    Args:
        pred_boxes: List of dicts, each containing "bbox": [xmin, ymin, xmax, ymax]
        gt_boxes: List of dicts, each containing "bbox": [xmin, ymin, xmax, ymax]
        iou_threshold: Minimum IoU threshold to consider a match.

    Returns:
        A tuple containing:
            - matches: List of tuples (pred_idx, gt_idx)
            - unmatched_preds: List of indices of unmatched predictions
            - unmatched_gts: List of indices of unmatched ground truths
    """
    pairs = []
    for p_idx, pred in enumerate(pred_boxes):
        for g_idx, gt in enumerate(gt_boxes):
            iou = compute_iou(pred["bbox"], gt["bbox"])
            if iou >= iou_threshold:
                pairs.append((iou, p_idx, g_idx))

    # Sort pairs by IoU in descending order
    pairs.sort(key=lambda x: x[0], reverse=True)

    matched_preds = set()
    matched_gts = set()
    matches = []

    for _iou, p_idx, g_idx in pairs:
        if p_idx not in matched_preds and g_idx not in matched_gts:
            matched_preds.add(p_idx)
            matched_gts.add(g_idx)
            matches.append((p_idx, g_idx))

    unmatched_preds = [i for i in range(len(pred_boxes)) if i not in matched_preds]
    unmatched_gts = [i for i in range(len(gt_boxes)) if i not in matched_gts]

    return matches, unmatched_preds, unmatched_gts
