"""Lazy engine creation — configurable via DATABASE_URL env var.

24/7 operational hardening:
- Pool pre-ping: validates connections before use
- Pool recycle: closes connections after 30 min to prevent stale connections
- Pool size from env (DB_POOL_SIZE, DB_POOL_OVERFLOW, DB_POOL_RECYCLE)
- Thread-safe engine singleton via lru_cache
- SQLite: WAL + busy timeout + process-wide write lock for multi-camera StorageWorkers
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from functools import lru_cache
from typing import Any, Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

# Serialize writers across always-on camera StorageWorkers (SQLite only).
_sqlite_write_lock = threading.RLock()


@lru_cache(maxsize=1)
def get_engine() -> Any:
    url = os.getenv("DATABASE_URL", "sqlite:///./data/trafficflow.db")
    connect_args: dict[str, Any] = {}
    if url.startswith("sqlite"):
        # Multiple always-on StorageWorkers + API readers contend on one file.
        # WAL + busy timeout keeps line-crossing inserts from "database is locked".
        connect_args["check_same_thread"] = False
        connect_args["timeout"] = float(os.getenv("SQLITE_TIMEOUT_SEC", "30"))

    engine_kwargs: dict[str, Any] = {
        "connect_args": connect_args,
        "pool_pre_ping": True,
    }

    pool_recycle = int(os.getenv("DB_POOL_RECYCLE", "1800"))
    engine_kwargs["pool_recycle"] = pool_recycle

    if not url.startswith("sqlite"):
        pool_size = int(os.getenv("DB_POOL_SIZE", "10"))
        max_overflow = int(os.getenv("DB_POOL_OVERFLOW", "5"))
        engine_kwargs["pool_size"] = max(1, min(pool_size, 100))
        engine_kwargs["max_overflow"] = max(0, min(max_overflow, 50))

    engine = create_engine(url, **engine_kwargs)

    if url.startswith("sqlite"):
        from sqlalchemy import event, text

        @event.listens_for(engine, "connect")
        def _sqlite_on_connect(dbapi_conn, _connection_record):  # type: ignore[no-untyped-def]
            cursor = dbapi_conn.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()

        # Apply once on the pool so the first connections inherit WAL.
        with engine.connect() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL"))
            conn.execute(text("PRAGMA synchronous=NORMAL"))
            conn.commit()

    return engine


def is_sqlite() -> bool:
    url = os.getenv("DATABASE_URL", "sqlite:///./data/trafficflow.db")
    return url.startswith("sqlite")


@contextmanager
def db_write_lock() -> Iterator[None]:
    """Hold while performing SQLite write transactions across threads."""
    if not is_sqlite():
        yield
        return
    with _sqlite_write_lock:
        yield


def commit_with_retry(
    session: Session,
    *,
    attempts: int = 8,
    base_delay: float = 0.05,
    already_locked: bool = False,
) -> None:
    """Commit with retry on transient SQLite lock errors.

    ``already_locked=True`` when the caller already holds ``db_write_lock``
    (avoids re-entrant lock + sleep while holding it for long).
    """
    last_exc: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            if already_locked or not is_sqlite():
                session.commit()
            else:
                with db_write_lock():
                    session.commit()
            return
        except Exception as exc:  # noqa: BLE001 — retry only locked/busy
            last_exc = exc
            msg = str(exc).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            session.rollback()
            time.sleep(base_delay * (2 ** i))
    if last_exc is not None:
        raise last_exc


def SessionLocal() -> Session:
    """Create a new session bound to the current (cached) engine."""
    return sessionmaker(bind=get_engine(), autocommit=False, autoflush=False)()


def get_session():
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
