import re
from pathlib import Path

_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def validate_identifier(value: str, name: str = "id") -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ValueError(f"Invalid {name}: {value!r}")
    return value


def safe_join(base: Path, *parts: str) -> Path:
    base_resolved = base.resolve()
    if not base_resolved.exists():
        base_resolved.mkdir(parents=True, exist_ok=True)
    p = base_resolved.joinpath(*parts).resolve()
    try:
        p.relative_to(base_resolved)
    except ValueError:
        raise ValueError(f"Path escapes base: {p}") from None
    return p
