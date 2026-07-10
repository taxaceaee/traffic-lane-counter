import csv
import json
from pathlib import Path
from typing import Any


class OutputWriter:
    """Manages writing frame data (JSONL), occupancy stats (CSV), lane-change logs (CSV),
    and per-crossing counts plus a final counts summary."""
    def __init__(self, output_dir: str | Path, lane_ids: list[str], fps: float = 25.0):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.lane_ids = lane_ids
        self.fps = fps

        self.jsonl_path = self.output_dir / "frames.jsonl"
        self.occ_path = self.output_dir / "occupancy.csv"
        self.lc_path = self.output_dir / "lane_changes.csv"
        self.counts_path = self.output_dir / "counts.csv"
        self.counts_summary_path = self.output_dir / "counts_summary.csv"

        # Write headers
        with open(self.occ_path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(["frame", *self.lane_ids])

        with open(self.lc_path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(["frame", "track_id", "class_name", "previous_stable_lane", "current_stable_lane"])

        with open(self.counts_path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(["frame", "line_id", "lane_id", "track_id", "class_name", "direction"])

    def write_frame(
        self,
        frame_id: int,
        tracks: list[dict[str, Any]],
        occupancy: dict[str, int],
        events: list[dict[str, Any]],
        raw_detections: list[dict[str, Any]] | None = None,
        crossings: list[dict[str, Any]] | None = None,
    ):
        """Writes frame status to JSONL and occupancy details to CSV.

        `crossings` is a list of per-frame crossing events (same shape as written to counts.csv).
        """
        timestamp = frame_id / self.fps
        frame_data = {
            "frame_id": frame_id,
            "timestamp": round(timestamp, 3),
            "detections": raw_detections if raw_detections is not None else [],
            "tracks": tracks,
            "occupancy": occupancy,
            "events": events,
            "crossings": crossings if crossings is not None else [],
        }
        with open(self.jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(frame_data) + "\n")

        occ_row = [frame_id] + [occupancy.get(lane_id, 0) for lane_id in self.lane_ids]
        with open(self.occ_path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(occ_row)

        if crossings:
            with open(self.counts_path, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                for ev in crossings:
                    w.writerow([
                        ev["frame"],
                        ev["line_id"],
                        ev["lane_id"],
                        ev["track_id"],
                        ev["class_name"],
                        ev["direction"],
                    ])

    def write_lane_change(self, frame_id: int, track_id: int, class_name: str, prev_lane: str, curr_lane: str):
        """Writes lane change event to lane_changes.csv."""
        with open(self.lc_path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([frame_id, track_id, class_name, prev_lane, curr_lane])

    def write_counts_summary(self, counts: dict[str, dict[str, dict[str, int]]]):
        """Writes the cumulative tally as counts_summary.csv. Called from close()."""
        with open(self.counts_summary_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["lane_id", "class_name", "direction", "count"])
            for lane_id in sorted(counts.keys()):
                for class_name in sorted(counts[lane_id].keys()):
                    for direction in ("forward", "backward"):
                        c = counts[lane_id][class_name].get(direction, 0)
                        w.writerow([lane_id, class_name, direction, c])

    def close(self, counts: dict[str, dict[str, dict[str, int]]] | None = None):
        """Closes all file handles. If `counts` provided, also writes counts_summary.csv."""
        if counts is not None:
            self.write_counts_summary(counts)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
