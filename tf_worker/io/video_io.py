import contextlib
import logging
import os
import random
import threading
import time
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Any

import cv2

from tf_worker.io.image_sequence import ImageSequenceReader

logger = logging.getLogger("trafficflow.video_io")

# RTSP reconnection: jittered exponential backoff prevents reconnect storms
# when multiple cameras lose connection simultaneously.
_RTSP_BASE_DELAY = 1.0
_RTSP_MAX_DELAY = 30.0
_RTSP_JITTER = 0.5


class VideoFileReader:
    """Wrapper around cv2.VideoCapture to provide a uniform interface."""
    def __init__(self, file_path: str | Path):
        self.file_path = str(file_path)
        self.cap = cv2.VideoCapture(self.file_path)
        if not self.cap.isOpened():
            raise ValueError(f"Could not open video file: {self.file_path}")
        self.width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.fps = float(self.cap.get(cv2.CAP_PROP_FPS))
        self.frame_count = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))

    def read(self) -> tuple[bool, Any]:
        return self.cap.read()

    def release(self):
        self.cap.release()

    def isOpened(self) -> bool:
        return self.cap.isOpened()

    @property
    def get_width(self) -> int:
        return self.width

    @property
    def get_height(self) -> int:
        return self.height

    @property
    def get_fps(self) -> float:
        return self.fps

    @property
    def get_frame_count(self) -> int:
        return self.frame_count


class CameraStreamReader:
    """Non-blocking RTSP/webcam reader that always delivers the latest frame.

    A background daemon thread continuously reads frames from the capture device
    and places them in a maxsize=1 queue.  When the inference loop calls read(),
    it gets the newest frame immediately — stale frames are discarded automatically
    because put_nowait() raises Full and we discard-then-replace.

    This breaks the camera-buffer → inference coupling that causes latency to
    accumulate on slow GPUs.  End-to-end latency improvement is typically
    50-200 ms compared to sequential cap.read() in the main loop.

    Config knobs (under ``input``):
        buffer_size (int, default 1): cv2 internal ring-buffer size.
        rtsp_transport (str, default "tcp"): "tcp" or "udp".
        target_fps (int, optional): cap frame rate read from camera; useful when
            the camera streams at 30 fps but inference only needs 20 fps.
    """

    def __init__(self, source: str, config: dict):
        self._source = source
        input_cfg = config.get("input", {})
        buffer_size = input_cfg.get("buffer_size", 1)
        rtsp_transport = input_cfg.get("rtsp_transport", "tcp")
        self._target_fps: float | None = input_cfg.get("target_fps")

        # Set RTSP transport env before constructing VideoCapture — FFmpeg reads it at open time.
        # Override unconditionally (not setdefault) to avoid another thread's setting taking effect.
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = f"rtsp_transport;{rtsp_transport}"
        self.cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, buffer_size)

        if not self.cap.isOpened():
            raise ValueError(f"Could not open camera stream: {source}")

        self.width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.fps = float(self.cap.get(cv2.CAP_PROP_FPS)) or 25.0
        # Live streams have no fixed frame count
        self.frame_count = -1

        self._queue: Queue = Queue(maxsize=1)
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._capture_loop, daemon=True, name="CameraStreamReader")
        self._thread.start()

    def _capture_loop(self) -> None:
        min_interval = (1.0 / self._target_fps) if self._target_fps else 0.0
        last_read = 0.0
        reconnect_delay = _RTSP_BASE_DELAY

        while not self._stop_event.is_set():
            now = time.monotonic()
            if now - last_read < min_interval:
                time.sleep(min_interval - (now - last_read))
            success, frame = self.cap.read()
            last_read = time.monotonic()
            if not success:
                # Exponential backoff + jitter to prevent reconnection storms
                jitter = reconnect_delay * _RTSP_JITTER
                sleep_time = reconnect_delay + random.uniform(-jitter, jitter)
                logger.warning(
                    "CameraStreamReader: read failed for %s — reconnecting in %.1fs",
                    self._source, sleep_time,
                )
                # Signal loss once
                with contextlib.suppress(ValueError, Empty, Full, OSError):
                    self._queue.put_nowait(None)
                time.sleep(max(0.1, sleep_time))

                # Attempt reconnect with exponential backoff
                old_cap = self.cap
                new_cap = cv2.VideoCapture(self._source, cv2.CAP_FFMPEG)
                if new_cap.isOpened():
                    new_cap.set(cv2.CAP_PROP_BUFFERSIZE, self.cap.get(cv2.CAP_PROP_BUFFERSIZE) or 1)
                    self.cap = new_cap
                    old_cap.release()
                    reconnect_delay = _RTSP_BASE_DELAY  # reset on success
                    continue
                new_cap.release()
                reconnect_delay = min(reconnect_delay * 2, _RTSP_MAX_DELAY)
                continue

            # Reset backoff on successful read
            reconnect_delay = _RTSP_BASE_DELAY

            # Discard stale frame, keep only the newest
            try:
                self._queue.put_nowait(frame)
            except (ValueError, Empty, OSError):
                with contextlib.suppress(Empty):
                    self._queue.get_nowait()
                with contextlib.suppress(ValueError, Empty, OSError):
                    self._queue.put_nowait(frame)

    def read(self) -> tuple[bool, Any]:
        """Block until a new frame is available (max 2 s) and return (success, frame)."""
        try:
            frame = self._queue.get(timeout=2.0)
            if frame is None:
                return False, None
            return True, frame
        except Empty:
            return False, None

    def release(self):
        self._stop_event.set()
        self._thread.join(timeout=3.0)
        self.cap.release()

    def isOpened(self) -> bool:
        return self.cap.isOpened()

    @property
    def get_width(self) -> int:
        return self.width

    @property
    def get_height(self) -> int:
        return self.height

    @property
    def get_fps(self) -> float:
        return self.fps

    @property
    def get_frame_count(self) -> int:
        return self.frame_count


class YouTubeLiveReader:
    """YouTube video/live-stream reader with auto URL refresh.

    Two modes:
      - ``youtube`` (one-shot): download entire video to temp file, then delegate
        to ``VideoFileReader``.  Cleaned up on ``.release()``.
      - ``youtube_live`` (live stream): resolve HLS URL, open via
        ``cv2.VideoCapture(FFMPEG)``, and periodically re-resolve the URL
        (YouTube HLS auth tokens expire every ~6--12 hours).

    Config knobs (under ``input`` in config YAML)::

        yt_format (str):           yt-dlp format string  (default ``best[height<=720]``)
        yt_refresh_interval (int): seconds between HLS URL refreshes  (default 14400)
        buffer_size (int):         cv2 internal ring-buffer  (default 1)
        target_fps (float):        throttle capture loop  (default unset)
    """

    MODE_ONE_SHOT = "youtube"
    MODE_LIVE = "youtube_live"

    def __init__(self, source: str, config: dict):
        self._source_url = str(source)
        self._config = config
        input_cfg = config.get("input", {})
        self._format: str = input_cfg.get("yt_format", "best[height<=720]")
        self._mode: str = input_cfg.get("source_type", self.MODE_ONE_SHOT)

        # Delegates
        self._delegate: VideoFileReader | None = None   # one-shot
        self._capture: cv2.VideoCapture | None = None   # live
        self._queue: Queue | None = None
        self._stop_event = threading.Event()
        self._capture_thread: threading.Thread | None = None
        self._refresh_thread: threading.Thread | None = None
        self._temp_file: str | None = None

        # Resolve stream info from yt-dlp
        self._stream_info = self._resolve_stream_info()

        if self._mode == self.MODE_ONE_SHOT:
            self._setup_one_shot()
        else:
            self._setup_live()

    # --- yt-dlp helpers (delegated to shared/yt_utils) -------------------------

    def _resolve_stream_info(self) -> dict:
        """Resolve YouTube URL via shared yt-dlp helper (cookie+retry)."""
        from shared.yt_utils import resolve_stream_info as _resolve_yt

        info = _resolve_yt(self._source_url, fmt=self._format, retries=3)
        return dict(info)

    # --- setup helpers -----------------------------------------------------------

    def _setup_one_shot(self) -> None:
        """Download video to a temp file and wrap with ``VideoFileReader``."""
        import tempfile

        from shared.yt_utils import download_video as _download_yt

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp_path = tmp.name

        _download_yt(self._source_url, tmp_path, fmt=self._format)

        self._temp_file = tmp_path
        self._delegate = VideoFileReader(tmp_path)

    def _setup_live(self) -> None:
        """Open ``cv2.VideoCapture`` on the resolved HLS URL and start threads."""
        stream_url = self._stream_info["url"]
        self._capture = cv2.VideoCapture(stream_url, cv2.CAP_FFMPEG)
        if not self._capture.isOpened():
            raise ValueError(f"Could not open YouTube live stream: {self._source_url}")

        input_cfg = self._config.get("input", {})
        buffer_size = input_cfg.get("buffer_size", 1)
        self._capture.set(cv2.CAP_PROP_BUFFERSIZE, buffer_size)

        self._width = int(self._capture.get(cv2.CAP_PROP_FRAME_WIDTH)) or self._stream_info["width"] or 0
        self._height = int(self._capture.get(cv2.CAP_PROP_FRAME_HEIGHT)) or self._stream_info["height"] or 0
        self._fps = float(self._capture.get(cv2.CAP_PROP_FPS)) or self._stream_info["fps"] or 25.0

        self._queue = Queue(maxsize=1)
        self._target_fps: float | None = input_cfg.get("target_fps")

        self._capture_thread = threading.Thread(
            target=self._capture_loop, daemon=True, name="YouTubeLiveReader-capture"
        )
        self._capture_thread.start()

        refresh_interval = input_cfg.get("yt_refresh_interval", 14400)
        self._refresh_thread = threading.Thread(
            target=self._refresh_loop,
            daemon=True,
            name="YouTubeLiveReader-refresh",
            args=(refresh_interval,),
        )
        self._refresh_thread.start()

    # --- background loops --------------------------------------------------------

    def _capture_loop(self) -> None:
        """Continuously read frames from capture into maxsize-1 queue."""
        min_interval = (1.0 / self._target_fps) if self._target_fps else 0.0
        last_read = 0.0

        while not self._stop_event.is_set():
            cap = self._capture
            if cap is None or not cap.isOpened():
                time.sleep(1.0)
                continue

            now = time.monotonic()
            if now - last_read < min_interval:
                time.sleep(min_interval - (now - last_read))

            success, frame = cap.read()
            last_read = time.monotonic()

            if not success:
                with contextlib.suppress(ValueError, Empty, Full, OSError):
                    self._queue.put_nowait(None)
                break

            # Keep only the newest frame
            try:
                self._queue.put_nowait(frame)
            except (ValueError, Empty, Full, OSError, AttributeError):
                with contextlib.suppress(Empty):
                    self._queue.get_nowait()
                with contextlib.suppress(ValueError, Empty, Full, OSError, AttributeError):
                    self._queue.put_nowait(frame)

    def _refresh_loop(self, interval: int) -> None:
        """Periodically re-resolve the HLS URL (YouTube auth tokens expire)."""
        while not self._stop_event.is_set():
            time.sleep(interval)
            if self._stop_event.is_set():
                break

            try:
                new_info = self._resolve_stream_info()
                new_url = new_info.get("url")
                if new_url and new_url != self._stream_info.get("url"):
                    self._swap_capture(new_url)
                    self._stream_info = new_info
                    logger.info("YouTubeLiveReader: refreshed HLS URL")
            except Exception:
                logger.warning("YouTubeLiveReader: failed to refresh HLS URL, retry in 30s")
                time.sleep(30)

    def _swap_capture(self, new_url: str) -> None:
        """Atomically replace the capture handle -- drain queue, open new, swap."""
        if self._capture:
            try:
                while True:
                    self._queue.get_nowait()
            except Empty:
                pass

        old_cap = self._capture
        new_cap = cv2.VideoCapture(new_url, cv2.CAP_FFMPEG)
        if new_cap.isOpened():
            self._capture = new_cap
            if old_cap:
                old_cap.release()
        else:
            logger.warning("YouTubeLiveReader: new capture failed -- keeping old one")
            new_cap.release()

    # --- public interface --------------------------------------------------------

    def read(self) -> tuple[bool, Any]:
        if self._mode == self.MODE_ONE_SHOT and self._delegate is not None:
            return self._delegate.read()

        if self._queue is None:
            return False, None

        try:
            frame = self._queue.get(timeout=2.0)
            if frame is None:
                return False, None
            return True, frame
        except Empty:
            return False, None

    def release(self) -> None:
        self._stop_event.set()

        if self._capture_thread is not None:
            self._capture_thread.join(timeout=3.0)
        if self._refresh_thread is not None:
            self._refresh_thread.join(timeout=3.0)
        if self._capture is not None:
            self._capture.release()
            self._capture = None
        if self._delegate is not None:
            self._delegate.release()
            self._delegate = None
        if self._temp_file is not None:
            try:
                Path(self._temp_file).unlink(missing_ok=True)
            except (FileNotFoundError, PermissionError, OSError):
                logger.warning("release: failed to delete temp file %s", self._temp_file)
            self._temp_file = None

    def isOpened(self) -> bool:
        if self._delegate is not None:
            return self._delegate.isOpened()
        return self._capture is not None and self._capture.isOpened()

    @property
    def get_width(self) -> int:
        if self._delegate is not None:
            return self._delegate.get_width
        return self._width

    @property
    def get_height(self) -> int:
        if self._delegate is not None:
            return self._delegate.get_height
        return self._height

    @property
    def get_fps(self) -> float:
        if self._delegate is not None:
            return self._delegate.get_fps
        return self._fps

    @property
    def get_frame_count(self) -> int:
        if self._delegate is not None:
            return self._delegate.get_frame_count
        return -1  # live


def get_video_reader(
    source: str | Path, config: dict
) -> VideoFileReader | ImageSequenceReader | CameraStreamReader | YouTubeLiveReader:
    """Factory that returns the appropriate reader based on source type.

    source_type options (set under ``input`` in config YAML):
        auto        -- infer from source path (directory -> image_seq, file -> video)
        image_dir   -- ImageSequenceReader
        video       -- VideoFileReader (local file)
        rtsp        -- CameraStreamReader (live RTSP / webcam URL)
        youtube     -- YouTubeLiveReader (one-shot: download -> process)
        youtube_live -- YouTubeLiveReader (live HLS streaming)
    """
    source_type = config.get("input", {}).get("source_type", "auto")

    # YouTube sources -- no filesystem check needed
    if source_type in ("youtube", "youtube_live"):
        return YouTubeLiveReader(str(source), config)

    # Live RTSP / webcam -- does not need to exist as a filesystem path
    if source_type == "rtsp":
        return CameraStreamReader(str(source), config)

    source_path = Path(source)
    if not source_path.exists():
        raise FileNotFoundError(f"Source path does not exist: {source_path}")

    if source_type == "image_dir" or (source_type == "auto" and source_path.is_dir()):
        extensions = config.get("input", {}).get("image_extensions")
        fps = config.get("input", {}).get("fps", 25.0)
        return ImageSequenceReader(source_path, extensions=extensions, fps=fps)

    if source_type == "video" or (source_type == "auto" and source_path.is_file()):
        return VideoFileReader(source_path)

    raise ValueError(
        f"Unable to determine reader type for source '{source}' and source_type '{source_type}'"
    )


def get_video_writer(output_path: str | Path, width: int, height: int, fps: float) -> cv2.VideoWriter:
    """Factory function to construct a cv2.VideoWriter for MP4 output."""
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    writer = cv2.VideoWriter(str(out_path), fourcc, fps, (width, height))
    if not writer.isOpened():
        raise ValueError(f"Could not open VideoWriter for path: {out_path}")
    return writer
