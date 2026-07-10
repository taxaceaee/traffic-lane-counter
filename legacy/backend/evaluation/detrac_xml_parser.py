from pathlib import Path
from typing import Any

try:
    import defusedxml.ElementTree as ET
except ImportError:
    import xml.etree.ElementTree as ET


def _safe_int(value, name: str) -> int:
    if value is None:
        raise ValueError(f"Missing XML attribute: {name}")
    try:
        return int(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid {name}: {value!r}") from exc


def _safe_float(value, name: str) -> float:
    if value is None:
        raise ValueError(f"Missing XML attribute: {name}")
    try:
        return float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid {name}: {value!r}") from exc


def parse_detrac_xml(xml_path: str | Path) -> dict[int, list[dict[str, Any]]]:
    """Parses UA-DETRAC XML annotation files.

    Args:
        xml_path: Path to the XML file.

    Returns:
        A dictionary mapping frame numbers (1-indexed int) to lists of target dictionaries.
        Each target dict has:
            - target_id: int
            - class_name: str
            - bbox: [xmin, ymin, xmax, ymax] (list of float)
    """
    xml_path = Path(xml_path)
    if not xml_path.exists():
        raise FileNotFoundError(f"UA-DETRAC XML file not found at: {xml_path}")

    tree = ET.parse(xml_path)
    root = tree.getroot()

    frame_targets = {}

    for frame in root.findall(".//frame"):
        try:
            frame_num = _safe_int(frame.get("num"), "frame.num")
        except ValueError:
            continue
        targets = []

        target_list = frame.find("target_list")
        if target_list is not None:
            for target in target_list.findall("target"):
                try:
                    target_id = _safe_int(target.get("id"), "target.id")
                    box = target.find("box")
                    if box is None:
                        continue
                    left = _safe_float(box.get("left"), "box.left")
                    top = _safe_float(box.get("top"), "box.top")
                    width = _safe_float(box.get("width"), "box.width")
                    height = _safe_float(box.get("height"), "box.height")
                except ValueError:
                    continue

                xmin = left
                ymin = top
                xmax = left + width
                ymax = top + height
                bbox = [xmin, ymin, xmax, ymax]

                attr = target.find("attribute")
                class_name = "unknown"
                if attr is not None:
                    class_name = attr.get("vehicle_type", "unknown")

                targets.append({
                    "target_id": target_id,
                    "class_name": class_name,
                    "bbox": bbox,
                })

        frame_targets[frame_num] = targets

    return frame_targets
