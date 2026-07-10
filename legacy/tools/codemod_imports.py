#!/usr/bin/env python3
"""Codemod: rewrite all imports from old package names to new ones.

Mapping:
  shared.xxx          → tf_core.xxx
  backend.db.xxx      → tf_db.xxx
  backend.storage.repo_protocol  → tf_db.storage.repo_protocol
  backend.storage.crop_protocol  → tf_db.storage.crop_protocol
  backend.storage.pubsub_protocol → tf_db.storage.pubsub_protocol
  backend.storage_adapters       → tf_api.storage_adapters
  backend.serializer             → tf_common.serializer
  backend.io.safe_path           → tf_common.safe_path
  backend.services.circuit_breaker → tf_common.circuit_breaker
  backend.services.alert_service  → tf_common.alert_service
  backend.log_setup              → tf_common.log_setup
  backend.services.live_bus      → tf_common.live_bus
  backend.pubsub                 → tf_common.pubsub
  backend.monitoring.metrics     → tf_common.monitoring.metrics
  backend.monitoring.live_metrics → tf_common.monitoring.live_metrics
  backend.api.xxx                → tf_api.api.xxx
  backend.main                   → tf_api.main
  backend.config                 → tf_api.config
  backend.services.health_checker → tf_api.services.health_checker
  backend.monitoring.system_metrics → tf_api.monitoring.system_metrics
  backend.worker                 → tf_worker.worker
  backend.pipeline               → tf_worker.pipeline
  backend.storage.storage_worker → tf_worker.storage.storage_worker
  backend.storage.local_crop_storage → tf_worker.storage.local_crop_storage
  backend.storage.retention      → tf_worker.storage.retention
  backend.events.xxx             → tf_worker.events.xxx
  backend.io.video_io            → tf_worker.io.video_io
  backend.io.writers             → tf_worker.io.writers
  backend.io.image_sequence      → tf_worker.io.image_sequence
  backend.visualization.xxx      → tf_worker.visualization.xxx
  backend.detection.motion_detector → tf_worker.detection.motion_detector
  backend.evaluation.xxx         → tf_worker.evaluation.xxx
  backend.benchmark.xxx          → tf_worker.benchmark.xxx
  backend.counting.xxx           → tf_worker.counting.xxx
  backend.lanes.xxx              → tf_worker.lanes.xxx
  backend.occupancy.xxx          → tf_worker.occupancy.xxx
  backend.tracking.xxx           → tf_worker.tracking.xxx
  backend.detection_core         → tf_core.detection_core
  backend.detection.xxx          → tf_worker.detection.xxx
  backend.io.xxx                 → tf_worker.io.xxx
  backend.services.xxx           → tf_worker.services.xxx (if any remain)
  backend.monitoring.xxx         → tf_worker.monitoring.xxx (if any remain)
"""

import os
import re
import sys

# Order matters: longest prefix first to avoid partial matches
REPLACEMENTS = [
    # tf_db (exact module paths)
    ("from backend.storage.repo_protocol import", "from tf_db.storage.repo_protocol import"),
    ("from backend.storage.crop_protocol import", "from tf_db.storage.crop_protocol import"),
    ("from backend.storage.pubsub_protocol import", "from tf_db.storage.pubsub_protocol import"),
    ("import backend.storage.repo_protocol", "import tf_db.storage.repo_protocol"),
    ("import backend.storage.crop_protocol", "import tf_db.storage.crop_protocol"),
    ("import backend.storage.pubsub_protocol", "import tf_db.storage.pubsub_protocol"),

    # tf_common - exact module paths (longest first)
    ("from backend.monitoring.live_metrics import", "from tf_common.monitoring.live_metrics import"),
    ("from backend.monitoring.metrics import", "from tf_common.monitoring.metrics import"),
    ("from backend.services.circuit_breaker import", "from tf_common.circuit_breaker import"),
    ("from backend.services.alert_service import", "from tf_common.alert_service import"),
    ("from backend.services.live_bus import", "from tf_common.live_bus import"),
    ("from backend.io.safe_path import", "from tf_common.safe_path import"),
    ("from backend.serializer import", "from tf_common.serializer import"),
    ("from backend.log_setup import", "from tf_common.log_setup import"),
    ("from backend.pubsub import", "from tf_common.pubsub import"),
    ("import backend.serializer", "import tf_common.serializer"),
    ("import backend.log_setup", "import tf_common.log_setup"),
    ("import backend.pubsub", "import tf_common.pubsub"),

    # tf_api - exact module paths
    ("from backend.monitoring.system_metrics import", "from tf_api.monitoring.system_metrics import"),
    ("from backend.services.health_checker import", "from tf_api.services.health_checker import"),
    ("from backend.storage_adapters import", "from tf_api.storage_adapters import"),
    ("from backend.main import", "from tf_api.main import"),
    ("import backend.main", "import tf_api.main"),

    # Submodule imports (from backend.X import Y where X is the module)
    ("from backend.monitoring import ", "from tf_common.monitoring import "),
    ("from backend.io import ", "from tf_worker.io import "),
    ("from backend.services import ", "from tf_common import "),
    ("from backend.config import ", "from tf_api.config import "),

    # tf_worker - exact module paths (longest first)
    ("from backend.storage.local_crop_storage import", "from tf_worker.storage.local_crop_storage import"),
    ("from backend.storage.storage_worker import", "from tf_worker.storage.storage_worker import"),
    ("from backend.detection.motion_detector import", "from tf_worker.detection.motion_detector import"),
    ("from backend.io.image_sequence import", "from tf_worker.io.image_sequence import"),
    ("from backend.io.video_io import", "from tf_worker.io.video_io import"),
    ("from backend.io.writers import", "from tf_worker.io.writers import"),
    ("import backend.storage.storage_worker", "import tf_worker.storage.storage_worker"),
    ("import backend.pipeline", "import tf_worker.pipeline"),
    ("import backend.worker", "import tf_worker.worker"),
    ("from backend.pipeline import", "from tf_worker.pipeline import"),
    ("from backend.worker import", "from tf_worker.worker import"),
    ("from backend.storage.retention import", "from tf_worker.storage.retention import"),
    ("from backend.detection_core import", "from tf_core.detection_core import"),
    ("import backend.detection_core", "import tf_core.detection_core"),

    # Namespace prefixes (these get subpath appended, so order still matters)
    ("from backend.db.", "from tf_db."),
    ("import backend.db.", "import tf_db."),
    ("from backend.api.", "from tf_api.api."),
    ("import backend.api.", "import tf_api.api."),
    ("from backend.events.", "from tf_worker.events."),
    ("import backend.events.", "import tf_worker.events."),
    ("from backend.visualization.", "from tf_worker.visualization."),
    ("import backend.visualization.", "import tf_worker.visualization."),
    ("from backend.evaluation.", "from tf_worker.evaluation."),
    ("import backend.evaluation.", "import tf_worker.evaluation."),
    ("from backend.benchmark.", "from tf_worker.benchmark."),
    ("import backend.benchmark.", "import tf_worker.benchmark."),
    ("from backend.counting.", "from tf_worker.counting."),
    ("import backend.counting.", "import tf_worker.counting."),
    ("from backend.lanes.", "from tf_worker.lanes."),
    ("import backend.lanes.", "import tf_worker.lanes."),
    ("from backend.occupancy.", "from tf_worker.occupancy."),
    ("import backend.occupancy.", "import tf_worker.occupancy."),
    ("from backend.tracking.", "from tf_worker.tracking."),
    ("import backend.tracking.", "import tf_worker.tracking."),

    # Catch-all: backend.X → tf_worker.X (for remaining backend modules)
    # This must come LAST among backend rules, after more specific matches
    ("from backend.io.", "from tf_worker.io."),
    ("import backend.io.", "import tf_worker.io."),
    ("from backend.detection.", "from tf_worker.detection."),
    ("import backend.detection.", "import tf_worker.detection."),
    ("from backend.services.", "from tf_worker.services."),
    ("import backend.services.", "import tf_worker.services."),
    ("from backend.monitoring.", "from tf_worker.monitoring."),
    ("import backend.monitoring.", "import tf_worker.monitoring."),

    # shared → tf_core (must come after any shared-specific mappings above)
    ("from shared.", "from tf_core."),
    ("import shared.", "import tf_core."),
]


def codemod_file(filepath: str) -> bool:
    """Rewrite imports in a single .py file. Returns True if changes made."""
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    original = content
    for old, new in REPLACEMENTS:
        if old in content:
            content = content.replace(old, new)

    if content != original:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    return False


def collect_py_files(root_dir: str) -> list[str]:
    """Collect all .py files under root_dir."""
    files = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        # Skip __pycache__
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if fn.endswith(".py"):
                files.append(os.path.join(dirpath, fn))
    return files


def main():
    root = os.path.dirname(os.path.abspath(__file__))

    # Directories to process
    targets = [
        "tf_core",
        "tf_common",
        "tf_db",
        "tf_api",
        "tf_worker",
        "detection_server",
        "scripts",
        "tests",
    ]

    total_modified = 0
    for target in targets:
        target_path = os.path.join(root, target)
        if not os.path.isdir(target_path):
            print(f"  [SKIP] {target}/ not found")
            continue
        py_files = collect_py_files(target_path)
        modified = 0
        for fp in py_files:
            if codemod_file(fp):
                modified += 1
        total_modified += modified
        print(f"  {target}/: {modified}/{len(py_files)} files modified")

    print(f"\nTotal: {total_modified} files modified")


if __name__ == "__main__":
    main()
