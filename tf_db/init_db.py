"""Database schema bootstrap and migration helpers."""

import subprocess
import sys
from pathlib import Path

from tf_db.session import get_engine

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def create_tables() -> None:
    """Legacy local-test helper; production startup should call migrate()."""
    from tf_db.models import Base
    Base.metadata.create_all(bind=get_engine())


def migrate() -> None:
    """Apply committed Alembic revisions to the configured database."""
    # Run Alembic as a child process.  Calling Alembic's synchronous command
    # API from Uvicorn's lifespan can deadlock the startup handshake in some
    # event-loop/runtime combinations; the CLI keeps migration isolated.
    subprocess.run(  # noqa: S603 - executable and arguments are fixed constants
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(_PROJECT_ROOT / "alembic.ini"),
            "upgrade",
            "head",
        ],
        cwd=_PROJECT_ROOT,
        check=True,
    )
