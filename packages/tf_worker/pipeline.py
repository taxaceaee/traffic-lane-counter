"""TrafficFlowPipeline — orchestrates vehicle detection, tracking, counting.

Lifecycle
---------
``__init__`` stores configuration but does NOT load models or start threads.
``start()`` loads the detector and starts the StorageWorker background thread.
``run()`` executes the frame-processing loop (calls ``start()`` if needed).
``stop()`` stops background threads and releases resources.
``reset()`` resets internal state so the pipeline can be re-run.

Production hardening:
- Prometheus metrics per stage (frame_processing_seconds)
- Adaptive frame skipping based on storage backpressure + motion detection
- OpenCV memory leak mitigation via explicit ``del`` + periodic ``gc.collect()``
- Structured logging with extra fields

Callers MUST call ``stop()`` after ``run()`` to release resources.
"""
import gc
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Queue
from typing import Any

import cv2
from tqdm import tqdm

from tf_common.monitoring import metrics
from tf_core.detection_core import DetectionCore
from tf_core.io.config_loader import load_and_validate_config
from tf_core.roi import CropROI
from tf_worker.events.event_logger import EventLogger
from tf_worker.io.video_io import get_video_reader, get_video_writer
from tf_worker.io.writers import OutputWriter
from tf_worker.visualization.visualizer import Visualizer

logger = logging.getLogger(__name__)


class TrafficFlowPipeline:
    """Orchestrates the entire lane occupancy MVP pipeline from end to end.

    Composes ``DetectionCore`` for detection logic and adds storage, file
    output, visualization, and video I/O.

    Parameters
    ----------
    config_path:
        Path to compiled YAML pipeline config.
    output_dir:
        Directory for output artefacts (videos, CSVs, crops).
    source:
        Video file path or image directory path.
    adapter:
        Optional ``RepositoryBundle`` protocol adapter for DB persistence.
        When ``None`` (file-only mode), no database writes occur.
    detector:
        Optional pre-initialised detector.  When ``None`` the detector is
        loaded lazily on the first call to ``start()``.
    """

    def __init__(
        self,
        config_path: str | Path,
        output_dir: str | Path,
        source: str | Path,
        adapter=None,  # RepositoryBundle | None
        adapter_factory=None,
        detector=None,  # YoloDetectorWrapper | None
        crop_storage=None,  # CropStorage | None
        publisher=None,  # StreamPublisher | None
    ):
        self.config_path = Path(config_path)
        self.output_dir = Path(output_dir)
        if isinstance(source, str) and (source.startswith("http://") or source.startswith("https://")):
            self.source = source
        else:
            self.source = Path(source)
        self._adapter = adapter
        self._adapter_factory = adapter_factory
        self._injected_detector = detector
        self._crop_storage = crop_storage
        self._external_publisher = publisher

        # Load and validate config
        self.config = load_and_validate_config(self.config_path)

        # Parse frame dimensions
        self.config_width = self.config["frame_size"]["width"]
        self.config_height = self.config["frame_size"]["height"]

        # ROI crop: compute crop region from lane polygons and transform config
        # so DetectionCore processes only the relevant area.
        self._roi: CropROI | None = None
        if self.config.get("detector", {}).get("roi_crop", False):
            roi_padding = int(self.config.get("roi_padding", 80))
            try:
                self._roi = CropROI(
                    self.config["lanes"],
                    self.config["frame_size"],
                    padding=roi_padding,
                )
                crop_config = self._roi.transform_config(self.config)
                # Native ROI imgsz — no intentional downscale of the crop.
                crop_config.setdefault("detector", {})
                crop_config["detector"]["imgsz"] = self._roi.suggested_imgsz()
                self._core = DetectionCore(crop_config, detector=self._injected_detector)
                logger.info(
                    "ROI crop enabled: %s (area ratio: %.2f%%), imgsz=%d",
                    self._roi, self._roi.area_ratio * 100,
                    crop_config["detector"]["imgsz"],
                )
            except Exception:
                logger.warning("Failed to init CropROI — falling back to full frame", exc_info=True)
                self._roi = None
                self._core = DetectionCore(self.config, detector=self._injected_detector)
        else:
            # Detection core — created in start()
            self._core = DetectionCore(self.config, detector=self._injected_detector)

        # I/O sub-components — created in start()
        self.event_logger = None
        self.visualizer = None
        self._storage = None
        self._publisher = None

        # Lifecycle guards
        self._started = False
        self._ran = False

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def state_manager(self):
        """Access the detection core's state manager (for callers that need it)."""
        return self._core.state_manager

    @property
    def line_counter(self):
        """Access the detection core's line counter."""
        return self._core.line_counter

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Load model weights and start background threads.

        Safe to call multiple times — only the first call has an effect.
        """
        if self._started:
            return
        self._started = True

        self._core.start()

        self.event_logger = EventLogger(self.config)
        self.visualizer = Visualizer(self.config)

        # Redis publisher
        if self._external_publisher is not None:
            self._publisher = self._external_publisher
        else:
            from tf_common.pubsub import RedisPublisher

            redis_cfg = self.config.get("redis", {})
            self._publisher = (
                RedisPublisher(
                    host=redis_cfg.get("host"),
                    port=redis_cfg.get("port"),
                )
                if redis_cfg.get("enabled", True)
                else None
            )

        # StorageWorker
        from tf_worker.storage.storage_worker import StorageWorker

        storage_cfg = self.config.get("storage", {})
        storage_root = self.output_dir / "storage"
        self._storage = StorageWorker(
            storage_root=storage_root,
            adapter=self._adapter,
            adapter_factory=self._adapter_factory,
            crop_storage=self._crop_storage,
            config=storage_cfg,
            publisher=self._publisher,
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Stop background threads and release resources."""
        if self._storage is not None:
            self._storage.stop(timeout=timeout)
        if self._publisher is not None:
            self._publisher.close()

    def reset(self) -> None:
        """Reset internal state so the pipeline can be run again."""
        self.stop()
        self._core.reset()
        self.event_logger = None
        self.visualizer = None
        self._storage = None
        self._publisher = self._external_publisher
        self._started = False
        self._ran = False

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        """Executes the pipeline on the specified source.

        Automatically calls ``start()`` on first invocation.
        To run a second time, call ``reset()`` first.
        """
        if self._ran:
            raise RuntimeError(
                "Pipeline has already been run. Call reset() before re-running."
            )
        self._ran = True
        self.start()

        # 1. Initialize Reader
        reader = get_video_reader(self.source, self.config)

        source_w = reader.get_width
        source_h = reader.get_height
        allow_scaling = self.config.get("allow_scaling", False) or self.config.get("input", {}).get("allow_scaling", False)

        if (source_w != self.config_width or source_h != self.config_height) and not allow_scaling:
            reader.release()
            raise ValueError(
                f"Config frame_size {self.config_width}x{self.config_height} does not match "
                f"input frame {source_w}x{source_h}. "
                f"Set allow_scaling=true or update lane polygon coordinates."
            )

        fps = reader.get_fps
        total_frames = reader.get_frame_count

        # 2. Setup Writers
        save_video = self.config.get("output", {}).get("save_video", True)
        save_jsonl = self.config.get("output", {}).get("save_jsonl", True)
        save_csv = self.config.get("output", {}).get("save_csv", True)
        occupancy_interval = int(self.config.get("occupancy_snapshot_interval", 1))
        if occupancy_interval < 1:
            occupancy_interval = 1

        writer = None
        write_queue: Queue | None = None
        write_thread = None
        if save_jsonl or save_csv:
            lane_ids = [lane.id for lane in self._core.lane_assigner.lanes]
            writer = OutputWriter(self.output_dir, lane_ids, fps=fps)
            write_queue = Queue(maxsize=8)
            _write_error: list = []

            def _write_worker():
                try:
                    while True:
                        item = write_queue.get()
                        if item is None:
                            break
                        fi, ft, occ, ev, rd, cr = item
                        writer.write_frame(fi, ft, occ, ev, rd, crossings=cr)
                except Exception:
                    logger.exception("WriteWorker thread died — subsequent frames will be dropped")
                    _write_error.append(True)

            write_thread = threading.Thread(target=_write_worker, daemon=True, name="WriteWorker")
            write_thread.start()

        video_writer = None
        video_queue: Queue | None = None
        video_thread: threading.Thread | None = None
        output_w, output_h = source_w, source_h
        if save_video:
            # Output at SOURCE resolution — not config resolution — so the
            # annotated video preserves the original camera/image dimensions
            # (e.g. 1920x1080, 1280x720).  Internal processing (detection,
            # ROI, visualizer) runs at config resolution for lane-coordinate
            # stability; the annotated result is scaled back up before write.
            out_video_path = self.output_dir / "annotated.mp4"
            video_writer = get_video_writer(out_video_path, output_w, output_h, fps)
            video_queue = Queue(maxsize=8)

            def _video_worker():
                try:
                    while True:
                        item = video_queue.get()
                        if item is None:
                            break
                        video_writer.write(item)
                except Exception:
                    logger.exception("VideoWriter thread died — subsequent frames will be dropped")

            video_thread = threading.Thread(target=_video_worker, daemon=True, name="VideoWriter")
            video_thread.start()

        # Frame-accurate timestamp: baseline = start of video, then frame_idx / fps
        start_time = datetime.now(timezone.utc)

        # Initialize timing metrics
        timing_records: dict[str, list[float]] = {
            "read": [],
            "detect_track": [],
            "lane_assign": [],
            "occupancy": [],
            "counting": [],
            "visualize": [],
            "write": [],
        }

        logger.info(
            "Starting processing: source=%s (%dx%d at %.1f fps), scaling=%s",
            self.source, source_w, source_h, fps, allow_scaling,
        )

        # Motion detector for adaptive frame skip (static scenes)
        from tf_worker.detection.motion_detector import MotionDetector
        motion_detector = MotionDetector(
            threshold=float(self.config.get("motion_threshold", 0.03)),
        )

        camera_id = self.config.get("server", {}).get("camera_id", "unknown")

        frame_idx = 0
        pbar = tqdm(total=total_frames, desc="Processing frames")

        try:
            while True:
                t_read_start = time.perf_counter()
                success, frame = reader.read()
                t_read_end = time.perf_counter()

                timing_records["read"].append((t_read_end - t_read_start) * 1000.0)

                if not success or frame is None:
                    timing_records["read"].pop()
                    break

                frame_idx += 1

                if allow_scaling and (frame.shape[1] != self.config_width or frame.shape[0] != self.config_height):
                    t_scale_start = time.perf_counter()
                    frame = cv2.resize(frame, (self.config_width, self.config_height))
                    timing_records["read"][-1] += (time.perf_counter() - t_scale_start) * 1000.0

                # 3-5b. Detection pipeline (delegated to DetectionCore)
                frame_ts = start_time + timedelta(seconds=frame_idx / fps)

                # ROI crop: only process the lane region (smaller → faster inference)
                frame_roi = self._roi.crop(frame) if self._roi is not None else frame

                # Adaptive frame skip: skip inference on static scenes
                storage_ratio = self._storage.backpressure_ratio if self._storage else 0.0
                skip_inference = False
                if storage_ratio > 0.9 or not motion_detector.has_motion(frame_roi):
                    skip_inference = True

                if skip_inference:
                    detection = {
                        "frame_idx": frame_idx, "frame_timestamp": frame_ts,
                        "tracks": [], "raw_detections": [], "events": [],
                        "occupancy": {}, "crossings": [], "frame_tracks": [],
                        "timing_ms": {"detect_track": 0, "lane_assign": 0,
                                      "occupancy": 0, "counting": 0},
                    }
                else:
                    detection = self._core.process_frame(frame_roi, frame_idx=frame_idx, frame_timestamp=frame_ts)

                # Transform coordinates from crop space → original frame space
                if self._roi is not None:
                    fw, fh = self.config_width, self.config_height
                    for track in detection.get("tracks", []):
                        if "bbox" in track:
                            track["bbox"] = self._roi.to_original(track["bbox"], fw, fh)
                    for raw in detection.get("raw_detections", []):
                        if "bbox" in raw:
                            raw["bbox"] = self._roi.to_original(raw["bbox"], fw, fh)
                    for ft in detection.get("frame_tracks", []):
                        if "bbox" in ft:
                            ft["bbox"] = self._roi.to_original(ft["bbox"], fw, fh)
                    # Patch state_manager track states so visualizer and crop
                    # extraction use original-frame coordinates
                    for state in self._core.state_manager.track_states.values():
                        if state.bbox is not None and len(state.bbox) == 4:
                            state.bbox = self._roi.to_original(state.bbox, fw, fh)

                raw_detections = detection["raw_detections"]
                events = detection["events"]
                occupancy = detection["occupancy"]
                crossings = detection["crossings"]
                frame_tracks = detection["frame_tracks"]

                # Prometheus metrics
                detect_ms = detection["timing_ms"]["detect_track"]
                metrics.observe_frame(camera_id, "detect_track", detect_ms / 1000.0)
                metrics.observe_frame(camera_id, "lane_assign", detection["timing_ms"]["lane_assign"] / 1000.0)
                metrics.observe_frame(camera_id, "occupancy", detection["timing_ms"]["occupancy"] / 1000.0)
                metrics.observe_frame(camera_id, "counting", detection["timing_ms"]["counting"] / 1000.0)
                if crossings:
                    for cx in crossings:
                        metrics.count_event(camera_id, cx.get("lane_id", "unknown"), cx.get("direction", ""))

                timing_records["detect_track"].append(detect_ms)
                timing_records["lane_assign"].append(detection["timing_ms"]["lane_assign"])
                timing_records["occupancy"].append(detection["timing_ms"]["occupancy"])
                timing_records["counting"].append(detection["timing_ms"]["counting"])

                # Update queue depth metrics
                if self._storage is not None:
                    metrics.queue_depth.labels(camera_id=camera_id).set(
                        self._storage._queue.qsize()
                    )

                # Structured logging for crossings
                if crossings:
                    logger.info(
                        "Crossings: frame=%d count=%d camera=%s",
                        frame_idx, len(crossings), camera_id,
                        extra={"camera_id": camera_id, "frame_idx": frame_idx,
                               "event_count": len(crossings)},
                    )

                # Storage: enqueue crossings with frame-accurate timestamp
                if crossings:
                    for cx in crossings:
                        state = self._core.state_manager.track_states.get(cx.get("track_id"))
                        bbox = state.bbox if state is not None else None
                        camera_id = self.config.get("server", {}).get("camera_id", "unknown")
                        job_id = self.config.get("server", {}).get("job_id", "offline")

                        # Extract crop bytes before enqueue — never store full
                        # frame in StorageWorker queue (prevents OOM).
                        crop_bytes: bytes | None = None
                        if frame is not None and bbox is not None:
                            try:
                                x1, y1, x2, y2 = [int(v) for v in bbox]
                                h, w = frame.shape[:2]
                                x1, y1 = max(0, x1), max(0, y1)
                                x2, y2 = min(w, x2), min(h, y2)
                                if x2 > x1 and y2 > y1:
                                    crop = frame[y1:y2, x1:x2]
                                    _, encoded = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 60])
                                    crop_bytes = encoded.tobytes()
                            except Exception:
                                crop_bytes = None

                        self._storage.enqueue(
                            camera_id=camera_id,
                            job_id=job_id,
                            lane_id=cx.get("lane_id", "unknown"),
                            track_id=cx.get("track_id", -1),
                            vehicle_type=cx.get("class_name", "unknown"),
                            direction=cx.get("direction", ""),
                            confidence=cx.get("confidence", 0.0),
                            frame_id=frame_idx,
                            timestamp=frame_ts,
                            frame=None,
                            bbox=bbox,
                            crop_bytes=crop_bytes,
                        )
                        if self._publisher is not None:
                            self._publisher.publish_event("traffic:events", {
                                "camera_id": camera_id,
                                "job_id": job_id,
                                "lane_id": cx.get("lane_id", "unknown"),
                                "track_id": cx.get("track_id", -1),
                                "vehicle_type": cx.get("class_name", "unknown"),
                                "direction": cx.get("direction", ""),
                                "confidence": cx.get("confidence", 0.0),
                                "frame_id": frame_idx,
                                "timestamp": frame_ts,
                            })

                # 6. Log events; enqueue frame data for async disk write
                t_write_start = time.perf_counter()
                self.event_logger.log_events(events, writer=writer)
                if events and self._storage is not None:
                    for event in events:
                        self._storage.enqueue_lane_change(
                            camera_id=camera_id,
                            job_id=self.config.get("server", {}).get("job_id", "offline"),
                            track_id=event.get("track_id", -1),
                            class_name=event.get("class_name", "unknown"),
                            previous_lane_id=event.get("previous_stable_lane"),
                            current_lane_id=event.get("current_stable_lane", "unknown"),
                            frame_id=event.get("frame", frame_idx),
                            timestamp=frame_ts,
                        )
                if crossings:
                    self.event_logger.log_crossings(crossings)

                if writer is not None:
                    if _write_error:
                        logger.warning("WriteWorker dead — skipping write for frame %d", frame_idx)
                    else:
                        occ_data = occupancy if frame_idx % occupancy_interval == 0 else {}
                        try:
                            write_queue.put_nowait(
                                (frame_idx, frame_tracks, occ_data, events, raw_detections, crossings)
                            )
                        except Exception:  # queue.Full or OSError during serialization
                            logger.warning("Write queue full — dropping frame %d log record", frame_idx)
                t_write_end = time.perf_counter()
                timing_records["write"].append((t_write_end - t_write_start) * 1000.0)

                # 8. Render visualization
                t_vis_start = time.perf_counter()
                if video_writer is not None:
                    annotated_frame = self.visualizer.draw(
                        frame, frame_idx, self._core.state_manager, occupancy,
                        line_counter=self._core.line_counter,
                        crossings_this_frame=crossings,
                    )
                    # Upscale to source resolution so the output video
                    # preserves original camera resolution (e.g. 1920x1080).
                    if output_w != self.config_width or output_h != self.config_height:
                        annotated_frame = cv2.resize(
                            annotated_frame, (output_w, output_h),
                        )
                    if video_queue is not None:
                        try:
                            video_queue.put_nowait(annotated_frame)
                        except Exception:
                            logger.warning("Video queue full — dropping frame %d", frame_idx)
                    else:
                        video_writer.write(annotated_frame)
                t_vis_end = time.perf_counter()
                timing_records["visualize"].append((t_vis_end - t_vis_start) * 1000.0)

                # Release frame explicitly — OpenCV's Python bindings can leak
                # memory if frames aren't explicitly freed (Milestone XProtect
                # case: ~2 MB/frame leak at 25 FPS → 3 GB/hour).
                del frame
                if frame_idx % 1000 == 0:
                    gc.collect()

                pbar.update(1)

        finally:
            pbar.close()
            reader.release()
            if write_queue is not None and write_thread is not None:
                write_queue.put(None)
                write_thread.join()
            if video_queue is not None and video_thread is not None:
                video_queue.put(None)
                video_thread.join()
            if writer is not None:
                writer.close(counts=self._core.get_counts())
            if video_writer is not None:
                video_writer.release()

        # Calculate frame total latencies
        total_latencies = []
        for i in range(frame_idx):
            tot = (
                timing_records["read"][i]
                + timing_records["detect_track"][i]
                + timing_records["lane_assign"][i]
                + timing_records["occupancy"][i]
                + timing_records["counting"][i]
                + timing_records["write"][i]
                + timing_records["visualize"][i]
            )
            total_latencies.append(tot)

        logger.info(f"Finished processing! Outputs saved to {self.output_dir}")
        return {
            "total_frames": frame_idx,
            "output_dir": str(self.output_dir),
            "timing_records": timing_records,
            "total_latencies": total_latencies,
        }
