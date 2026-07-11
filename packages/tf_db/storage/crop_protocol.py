"""CropStorage protocol — pluggable vehicle-crop persistence.

Defines the interface that LocalCropStorage (local disk) and
S3CropStorage (MinIO / S3) both implement.  The AI pipeline depends
only on this protocol; concrete backends live in ``trafficflow_server``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class CropStorage(Protocol):
    """Save and delete vehicle-crop images.

    Implementations handle the actual image encoding (JPEG/WebP), resizing,
    and destination storage (local FS, S3, etc.).
    """

    def save(
        self,
        *,
        camera_id: str,
        lane_id: str,
        vehicle_type: str,
        track_id: int,
        timestamp: datetime,
        crop_bytes: bytes | None = None,  # pre-encoded JPEG bytes (PERF2)
        bbox: list[float] | None = None,
    ) -> str | None:
        """Persist a pre-encoded vehicle crop.

        ``crop_bytes`` is a JPEG-encoded crop extracted by the producer to
        avoid holding full BGR frames in the pipeline queue.

        Returns a *storage-relative* path (e.g. ``crops/…/img.jpg``) that
        can be stored in ``VehicleCountEvent.crop_path``.  Returns ``None``
        when the crop is empty or saving is disabled.
        """
        ...

    def delete_before(self, prefix: str, cutoff: datetime) -> int:
        """Delete all crops under ``prefix`` whose mtime < ``cutoff``.

        Returns the number of files deleted.
        """
        ...
