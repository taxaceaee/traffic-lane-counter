"""LocalCropStorage — saves vehicle crop images to the local filesystem.

Extracted from ``StorageWorker._save_crop()`` into a standalone adapter
that satisfies the ``CropStorage`` protocol, making it swappable with
S3 / MinIO backends without touching AI-pipeline code.
"""
from __future__ import annotations

import contextlib
import logging
from datetime import datetime
from pathlib import Path

import cv2

from tf_common.safe_path import safe_join, validate_identifier

logger = logging.getLogger("trafficflow.storage.local_crop_storage")


class LocalCropStorage:
    """Persist crop images under ``{storage_root}/crops/…``.

    Parameters
    ----------
    storage_root:
        Base directory; ``crops/`` will be created underneath it.
    format:
        ``"jpg"`` or ``"webp"``.
    quality:
        JPEG quality (0-100) or WebP quality (0-100), passed to OpenCV.
    max_px:
        Longest edge in pixels; crops are scaled down to fit.
    """

    def __init__(
        self,
        storage_root: str | Path,
        format: str = "jpg",
        quality: int = 80,
        max_px: int = 320,
    ):
        self.storage_root = Path(storage_root)
        self.format = format.lower()
        self.quality = quality
        self.max_px = max_px

    # ------------------------------------------------------------------
    # CropStorage protocol
    # ------------------------------------------------------------------

    def save(
        self,
        *,
        camera_id: str,
        lane_id: str,
        vehicle_type: str,
        track_id: int,
        timestamp: datetime,
        crop_bytes: bytes | None = None,
        bbox: list[float] | None = None,
    ) -> str | None:
        if crop_bytes is None:
            return None

        img = cv2.imdecode(
            __import__("numpy").frombuffer(crop_bytes, dtype=__import__("numpy").uint8),
            cv2.IMREAD_COLOR,
        )
        if img is None:
            return None
        ch, cw = img.shape[:2]
        scale = self.max_px / max(ch, cw)
        if scale < 1.0:
            crop = cv2.resize(img, (max(1, int(cw * scale)), max(1, int(ch * scale))))
        else:
            crop = img

        camera_id_safe = validate_identifier(camera_id, "camera_id")
        lane_id_safe = validate_identifier(lane_id, "lane_id")
        vehicle_type_safe = validate_identifier(vehicle_type, "vehicle_type")

        date_dir = safe_join(
            self.storage_root, "crops",
            timestamp.strftime("%Y/%m/%d"),
            camera_id_safe,
        )
        date_dir.mkdir(parents=True, exist_ok=True)

        ext = "jpg" if self.format == "jpg" else self.format
        fname = (
            f"{camera_id_safe}_{lane_id_safe}_{vehicle_type_safe}"
            f"_{timestamp.strftime('%Y%m%d_%H%M%S')}_{track_id}.{ext}"
        )
        fpath = safe_join(date_dir, fname)

        if self.format == "jpg":
            cv2.imwrite(str(fpath), crop, [cv2.IMWRITE_JPEG_QUALITY, self.quality])
        elif self.format == "webp":
            cv2.imwrite(str(fpath), crop, [cv2.IMWRITE_WEBP_QUALITY, self.quality])
        else:
            cv2.imwrite(str(fpath), crop)

        return str(fpath.relative_to(self.storage_root))

    def delete_before(self, prefix: str, cutoff: datetime) -> int:
        """Walk the crop tree under ``prefix`` and delete files older than cutoff."""
        root = safe_join(self.storage_root, prefix)
        if not root.exists():
            return 0
        cutoff_ts = cutoff.timestamp()
        deleted = 0
        for fpath in root.rglob("*"):
            if not fpath.is_file():
                continue
            try:
                if fpath.stat().st_mtime < cutoff_ts:
                    fpath.unlink()
                    deleted += 1
            except (FileNotFoundError, PermissionError, OSError) as exc:
                logger.warning("Could not delete %s: %s", fpath, exc.__class__.__name__)
        self._remove_empty_dirs(root)
        return deleted

    @staticmethod
    def _remove_empty_dirs(root: Path) -> None:
        for dirpath in sorted(root.rglob("*"), reverse=True):
            if dirpath.is_dir():
                with contextlib.suppress(OSError):
                    dirpath.rmdir()
