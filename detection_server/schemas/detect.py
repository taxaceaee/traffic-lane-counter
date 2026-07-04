"""Pydantic schemas for detection server requests and responses.

Lanes are NOT hardcoded — the caller (backend) sends lane config per camera
in every request, so lanes can change without restarting the detection server.
"""

from pydantic import BaseModel, Field


class CountingLineDef(BaseModel):
    start: list[float] = Field(..., min_length=2, max_length=2, description="[x, y] in original frame")
    end: list[float] = Field(..., min_length=2, max_length=2, description="[x, y] in original frame")
    direction_ref: list[float] = Field(..., min_length=2, max_length=2, description="[x, y] reference point")


class LaneDef(BaseModel):
    lane_id: str = Field(..., min_length=1, description="Unique lane identifier")
    name: str = ""
    polygon: list[list[float]] = Field(..., min_length=3, description="Polygon vertices [[x,y], ...]")
    counting_line: CountingLineDef | None = None


class ROIRequest(BaseModel):
    x1: int = Field(..., ge=0)
    y1: int = Field(..., ge=0)
    x2: int = Field(..., ge=0)
    y2: int = Field(..., ge=0)


class DetectRequest(BaseModel):
    """Configuration sent with each frame-detection request.

    The caller (backend server) loads camera + lanes config from YAML,
    then sends this structured payload alongside the raw image bytes.
    This decouples the detection server from file-based config storage.
    """
    camera_id: str = Field(..., min_length=1, description="Camera identifier — used to maintain per-camera track state")
    model_weights: str = Field(default="yolo11s", description="Model weights name (e.g. yolo11s, yolo11n)")
    imgsz: int = Field(default=640, ge=32, description="YOLO input size (longest side)")
    conf_threshold: float = Field(default=0.35, ge=0.0, le=1.0)
    iou_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    half: bool = Field(default=False, description="FP16 half-precision (GPU only)")
    allowed_classes: list[str] = Field(default=[], description="Filter: classes to detect (empty = all)")
    detect_every_n_frames: int = Field(default=1, ge=1, description="Run detection every N frames; track on others")
    min_track_age_frames: int = Field(default=3, ge=1, description="Min frames before counting")
    min_cross_distance_px: float = Field(default=2.0, ge=0.0, description="Hysteresis for counting-line detection")
    lanes: list[LaneDef] = Field(default=[], description="Lane polygons + counting lines")
    roi: ROIRequest | None = Field(default=None, description="Optional crop region [x1,y1,x2,y2]")


# ── Response models ──────────────────────────────────────────────────────

class TrackResult(BaseModel):
    track_id: int
    class_name: str
    confidence: float
    bbox: list[float]  # [x1, y1, x2, y2]
    lane_id: str | None = None


class RawDetectionResult(BaseModel):
    class_name: str
    confidence: float
    bbox: list[float]  # [x1, y1, x2, y2]


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
    direction: str  # "forward" | "backward"
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
    camera_id: str
    frame_idx: int
    frame_timestamp: str = ""
    tracks: list[TrackResult] = []
    raw_detections: list[RawDetectionResult] = []
    events: list[LaneChangeEvent] = []
    occupancy: dict[str, int] = {}
    crossings: list[CrossingResult] = []
    frame_tracks: list[FrameTrack] = []
    timing_ms: TimingMs


class StatusResponse(BaseModel):
    camera_id: str
    frame_idx: int
    track_count: int
    lane_count: int
    model_weights: str
