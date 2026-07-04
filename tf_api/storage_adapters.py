"""Concrete ``RepositoryBundle`` adapter — bridges protocol → SQLAlchemy ORM.

Usage::

    from tf_api.storage_adapters import make_server_adapters
    from tf_db.session import SessionLocal

    adapter = make_server_adapters(SessionLocal())
    StorageWorker(storage_root=..., adapter=adapter)
"""
from __future__ import annotations

from typing import Any

from tf_db.repositories import (
    SqlAggregateRepository,
    SqlCleanupRepository,
    SqlEventRepository,
)
from tf_db.storage.repo_protocol import RepositoryBundle


class _ServerAdapterBundle:
    """Composite ``RepositoryBundle`` implementation."""

    def __init__(self, session: Any) -> None:
        self._session = session
        self.events = SqlEventRepository(session)
        self.aggregates = SqlAggregateRepository(session)
        self.cleanup = SqlCleanupRepository(session)

    def close(self) -> None:
        self._session.close()


def make_server_adapters(session: Any) -> RepositoryBundle:
    """Create a ``RepositoryBundle``-compatible adapter from a DB session.

    Parameters
    ----------
    session:
        SQLAlchemy ``Session`` instance (e.g. ``SessionLocal()``).

    Returns
    -------
    RepositoryBundle
        Implements ``RepositoryBundle`` protocol.
    """
    return _ServerAdapterBundle(session)
