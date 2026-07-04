from pathlib import Path
from typing import Any

import yaml


def load_manifest(manifest_path: str | Path) -> dict[str, Any]:
    """Loads and validates a benchmark manifest YAML file.

    Args:
        manifest_path: Path to the manifest YAML file.

    Returns:
        The validated manifest dictionary.
    """
    manifest_path = Path(manifest_path)
    if not manifest_path.exists():
        raise FileNotFoundError(f"Benchmark manifest file not found at: {manifest_path}")

    with open(manifest_path, encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f)
        except Exception as e:
            raise ValueError(f"Failed to parse manifest YAML: {e}") from e

    if not isinstance(data, dict):
        raise ValueError("Manifest content must be a dictionary.")

    if "benchmark_name" not in data:
        raise ValueError("Manifest must contain 'benchmark_name'.")

    if "sequences" not in data or not isinstance(data["sequences"], list):
        raise ValueError("Manifest must contain a 'sequences' list.")

    for idx, seq in enumerate(data["sequences"]):
        if not isinstance(seq, dict):
            raise ValueError(f"Sequence entry at index {idx} must be a dictionary.")

        required_fields = ["sequence_id", "source", "lane_config"]
        for field in required_fields:
            if field not in seq:
                raise ValueError(f"Sequence entry {seq.get('sequence_id', f'at index {idx}')} is missing required field '{field}'.")

        # Validate paths
        source_path = Path(seq["source"])
        if not source_path.exists():
            raise FileNotFoundError(f"Sequence source path does not exist: {source_path}")

        config_path = Path(seq["lane_config"])
        if not config_path.exists():
            raise FileNotFoundError(f"Lane configuration file does not exist: {config_path}")

        if "gt_xml" in seq and seq["gt_xml"] is not None:
            xml_path = Path(seq["gt_xml"])
            if not xml_path.exists():
                raise FileNotFoundError(f"Ground truth XML file does not exist: {xml_path}")

    return data
