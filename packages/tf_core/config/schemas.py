from typing import Any, Literal

from pydantic import BaseModel, Field


class FrameSize(BaseModel):
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class AppConfig(BaseModel):
    app: dict[str, Any] = Field(default_factory=dict)
    database: dict[str, Any] = Field(default_factory=dict)
    storage: dict[str, Any] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)


class ModelVersion(BaseModel):
    model_id: str
    path: str
    class_mode: str
    description: str = ""


class ModelsConfig(BaseModel):
    models: list[ModelVersion]


class CameraSection(BaseModel):
    camera_id: str
    name: str = ""
    source_type: Literal["video", "image_dir", "rtsp", "youtube", "youtube_live"]
    source: str
    fps: float = Field(default=25.0, gt=0)
    frame_size: FrameSize
    allow_scaling: bool = False


class CameraModelSection(BaseModel):
    model_id: str
    conf_threshold: float = Field(default=0.35, ge=0.0, le=1.0)
    iou_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    imgsz: int = Field(default=960, gt=0)
    allowed_classes: list[str] | None = None


class CameraTrackerSection(BaseModel):
    type: str = "bytetrack"
    tracker: str = "bytetrack.yaml"
    track_timeout_frames: int = Field(default=10, gt=0)
    min_track_age_frames: int = Field(default=3, ge=0)


class CameraLanesSection(BaseModel):
    config_path: str


class CameraOccupancySection(BaseModel):
    history_window: int = Field(default=10, gt=0)
    min_consecutive_for_change: int = Field(default=5, gt=0)
    unknown_timeout_frames: int = Field(default=15, gt=0)


class CameraConfig(BaseModel):
    camera: CameraSection
    model: CameraModelSection
    tracker: CameraTrackerSection = Field(default_factory=CameraTrackerSection)
    lanes: CameraLanesSection
    occupancy: CameraOccupancySection = Field(default_factory=CameraOccupancySection)
    output: dict[str, Any] = Field(default_factory=dict)


class CountingLineDef(BaseModel):
    start: list[float]
    end: list[float]
    direction_ref: list[float]


class LaneItem(BaseModel):
    lane_id: str
    name: str = ""
    polygon: list[list[float]]
    counting_line: CountingLineDef | None = None


class LaneConfig(BaseModel):
    camera_id: str
    coordinate_space: Literal["original_frame"] = "original_frame"
    frame_size: FrameSize
    lanes: list[LaneItem]
