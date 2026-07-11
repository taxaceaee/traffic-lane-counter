"""Pydantic schemas for detection server requests and responses."""

from pydantic import BaseModel, Field


class CountingLineDef(BaseModel):
    start: list[float] = Field(..., min_length=2, max_length=2)
    end: list[float] = Field(..., min_length=2, max_length=2)
    direction_ref: list[float] = Field(..., min_length=2, max_length=2)


class LaneConfig(BaseModel):
    lane_id: str
    polygon: list[list[float]]
    counting_line: CountingLineDef | None = None


class DetectConfig(BaseModel):
    camera_id: str = "unknown"
    model_weights: str
    class_mode: str = "coco_pretrained"
    allowed_classes: list[str] = []
    conf_threshold: float = 0.25
    iou_threshold: float = 0.45
    imgsz: int = 1280
    half: bool = True
    lanes: list[LaneConfig] = []
    tracker_config: str = "bytetrack.yaml"
    detect_every_n_frames: int = 1
    min_track_age_frames: int = 2
    min_cross_distance_px: float = 2.0


class TrackResult(BaseModel):
    track_id: int
    class_name: str
    confidence: float
    bbox: list[float]


class FrameTrack(BaseModel):
    track_id: int
    class_name: str
    confidence: float
    bbox: list[float]
    raw_lane: str
    stable_lane: str | None
    is_counted_in_occupancy: bool


class CrossingResult(BaseModel):
    frame: int
    track_id: int
    class_name: str
    lane_id: str
    line_id: str
    direction: str
    confidence: float


class LaneChangeEvent(BaseModel):
    frame: int
    track_id: int
    class_name: str
    previous_stable_lane: str
    current_stable_lane: str


class TimingMs(BaseModel):
    detect_track: float
    lane_assign: float
    occupancy: float
    counting: float


class DetectResponse(BaseModel):
    frame_idx: int
    frame_timestamp: str = ""  # ISO-8601 UTC, e.g. "2026-07-01T12:34:56.789000+00:00"
    tracks: list[TrackResult] = []
    raw_detections: list[dict] = []
    events: list[LaneChangeEvent] = []
    occupancy: dict[str, int] = {}
    crossings: list[CrossingResult] = []
    frame_tracks: list[FrameTrack] = []
    timing_ms: TimingMs


class SessionInit(BaseModel):
    camera_id: str = "unknown"
    model_weights: str
    class_mode: str = "coco_pretrained"
    allowed_classes: list[str] = []
    conf_threshold: float = 0.25
    iou_threshold: float = 0.45
    imgsz: int = 1280
    half: bool = True
    lanes: list[LaneConfig] = []
    tracker_config: str = "bytetrack.yaml"
    detect_every_n_frames: int = 1
    min_track_age_frames: int = 2
    min_cross_distance_px: float = 2.0
