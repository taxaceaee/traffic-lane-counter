"""RetentionCleaner — purge old artefacts according to a configurable policy.

Typical YAML config block (under ``retention``):

    retention:
      crop_days: 7
      event_days: 30
      alert_clip_days: 90
      minute_agg_days: 180
      daily_agg_days: 0      # 0 = keep forever

Call ``RetentionCleaner(storage_root, db_factory, config).run()`` from a
scheduled job (cron, APScheduler, or a simple nightly thread).
"""

from __future__ import annotations

import logging
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("trafficflow.storage.retention")


class RetentionCleaner:
    """Deletes stale files and database rows per retention policy.

    Parameters
    ----------
    storage_root:
        Root of the file artefact tree produced by ``StorageWorker``.
    db_session_factory:
        Callable returning a SQLAlchemy ``Session``, or ``None`` to skip DB.
    config:
        Dict from the YAML ``retention`` block.  Recognised keys:

            crop_days      (int, default 7)
            event_days     (int, default 30)
            alert_clip_days(int, default 90)
            minute_agg_days(int, default 180)
            daily_agg_days (int, default 0 = keep forever)
    """

    def __init__(
        self,
        storage_root: str | Path,
        cleanup_repo: Any = None,  # CleanupRepository | None
        config: dict[str, Any] | None = None,
    ):
        self.storage_root = Path(storage_root)
        self.cleanup_repo = cleanup_repo  # CleanupRepository | None
        cfg = config or {}
        self.crop_days: int = int(cfg.get("crop_days", 7))
        self.event_days: int = int(cfg.get("event_days", 30))
        self.alert_clip_days: int = int(cfg.get("alert_clip_days", 90))
        self.minute_agg_days: int = int(cfg.get("minute_agg_days", 180))
        self.daily_agg_days: int = int(cfg.get("daily_agg_days", 0))

    def run(self) -> dict[str, int]:
        """Execute all retention passes and return counts of items deleted."""
        now = datetime.now(timezone.utc)
        stats: dict[str, int] = {}
        stats["crop_files"] = self._purge_files(
            self.storage_root / "crops", days=self.crop_days, now=now
        )
        stats["clip_files"] = self._purge_files(
            self.storage_root / "clips", days=self.alert_clip_days, now=now
        )
        if self.cleanup_repo is not None:
            stats["event_rows"] = self.cleanup_repo.delete_events_before(
                now - timedelta(days=self.event_days)
            ) if self.event_days > 0 else 0
            agg_deleted = 0
            if self.minute_agg_days > 0:
                cutoff = now - timedelta(days=self.minute_agg_days)
                agg_deleted += self.cleanup_repo.delete_aggregates_before(cutoff, "1min")
                agg_deleted += self.cleanup_repo.delete_aggregates_before(cutoff, "5min")
            if self.daily_agg_days > 0:
                cutoff = now - timedelta(days=self.daily_agg_days)
                agg_deleted += self.cleanup_repo.delete_aggregates_before(cutoff, "1hour")
                agg_deleted += self.cleanup_repo.delete_aggregates_before(cutoff, "1day")
            stats["aggregate_rows"] = agg_deleted
        logger.info("RetentionCleaner completed: %s", stats)
        return stats

    # ------------------------------------------------------------------
    # File purge
    # ------------------------------------------------------------------

    def _purge_files(self, directory: Path, days: int, now: datetime) -> int:
        """Delete files older than ``days`` under ``directory``.  Returns count."""
        if days <= 0 or not directory.exists():
            return 0
        cutoff = now - timedelta(days=days)
        cutoff_ts = cutoff.timestamp()
        deleted = 0
        for fpath in directory.rglob("*"):
            if not fpath.is_file():
                continue
            try:
                if fpath.stat().st_mtime < cutoff_ts:
                    fpath.unlink()
                    deleted += 1
            except (FileNotFoundError, PermissionError, OSError) as exc:
                logger.warning(
                    "Could not delete %s: %s",
                    fpath,
                    exc.__class__.__name__,
                )
        # Remove empty directories left behind
        self._remove_empty_dirs(directory)
        return deleted

    def _remove_empty_dirs(self, root: Path) -> None:
        for dirpath in sorted(root.rglob("*"), reverse=True):
            if dirpath.is_dir():
                with suppress(OSError):
                    dirpath.rmdir()   # only succeeds if empty

    # ------------------------------------------------------------------
    # DB row purge
    # ------------------------------------------------------------------

    # DB row purge is delegated to the CleanupRepository protocol adapter.
    # No ORM imports in this module — see repo_protocol.py for the interface
    # and storage_adapters.py for the server-side implementation.
