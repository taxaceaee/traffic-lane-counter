"""Lazy engine creation — configurable via DATABASE_URL env var.

24/7 operational hardening:
- Pool pre-ping: validates connections before use
- Pool recycle: closes connections after 30 min to prevent stale connections
- Pool size from env (DB_POOL_SIZE, DB_POOL_OVERFLOW, DB_POOL_RECYCLE)
- Thread-safe engine singleton via lru_cache
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


@lru_cache(maxsize=1)
def get_engine() -> Any:
    url = os.getenv("DATABASE_URL", "sqlite:///./data/trafficflow.db")
    connect_args: dict[str, Any] = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

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

    return create_engine(url, **engine_kwargs)


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
