import logging
from typing import Any

logger = logging.getLogger("TrafficFlow.Events")

class EventLogger:
    """Logs confirmed stable lane change events to console and records them to output files."""
    def __init__(self, config: dict):
        pass

    def log_events(self, events: list[dict[str, Any]], writer: Any | None = None):
        """Logs a list of lane change events.

        Args:
            events: A list of event dicts, each with:
                - frame (int)
                - track_id (int)
                - class_name (str)
                - previous_stable_lane (str)
                - current_stable_lane (str)
            writer: Optional OutputWriter to log the event to CSV.
        """
        for event in events:
            frame = event["frame"]
            tid = event["track_id"]
            cls_name = event["class_name"]
            prev_lane = event["previous_stable_lane"]
            curr_lane = event["current_stable_lane"]

            logger.info(
                "[Lane Change] Frame %d: Vehicle #%d (%s) switched %s -> %s",
                frame, tid, cls_name, prev_lane, curr_lane,
            )

            # Save to CSV using the writer
            if writer is not None:
                writer.write_lane_change(frame, tid, cls_name, prev_lane, curr_lane)

    def log_crossings(self, crossings: list[dict[str, Any]]):
        """Logs counting-line crossing events via logger.

        Args:
            crossings: A list of crossing event dicts, each with:
                - frame (int)
                - track_id (int)
                - class_name (str)
                - lane_id (str)
                - line_id (str)
                - direction (str): 'forward' or 'backward'
        """
        for ev in crossings:
            logger.info(
                "[Count] Frame %d: Vehicle #%d (%s) crossed %s %s",
                ev['frame'], ev['track_id'], ev['class_name'], ev['lane_id'], ev['direction'],
            )
