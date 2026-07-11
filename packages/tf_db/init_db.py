"""Database schema bootstrap and migration helpers."""

import os
import subprocess
import sys
from pathlib import Path

# packages/tf_db → packages → repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _REPO_ROOT / "services" / "database" / "alembic.ini"


def migrate() -> None:
    """Apply committed Alembic revisions to the configured database."""
    # Run Alembic as a child process.  Calling Alembic's synchronous command
    # API from Uvicorn's lifespan can deadlock the startup handshake in some
    # event-loop/runtime combinations; the CLI keeps migration isolated.
    env = os.environ.copy()
    packages = str(_REPO_ROOT / "packages")
    env["PYTHONPATH"] = packages + os.pathsep + env.get("PYTHONPATH", "")
    subprocess.run(  # noqa: S603 - executable and arguments are fixed constants
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(_ALEMBIC_INI),
            "upgrade",
            "head",
        ],
        cwd=_REPO_ROOT,
        env=env,
        check=True,
    )
