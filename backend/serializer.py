"""Fast JSON serialization — uses orjson (3-6x faster than stdlib) with fallback.

Usage:
    from backend.serializer import dumps, loads
    data = dumps({"camera_id": "CAM_01", "count": 42})
"""
from datetime import datetime
from typing import Any

try:
    import orjson

    def dumps(obj: Any, **kwargs: Any) -> str:
        def _default(o: Any) -> str:
            if isinstance(o, datetime):
                return o.isoformat()
            raise TypeError
        return orjson.dumps(obj, default=_default).decode("utf-8")

    def loads(s: str | bytes) -> Any:
        return orjson.loads(s)

except ImportError:
    import json

    def _default(o: Any) -> str:
        if isinstance(o, datetime):
            return o.isoformat()
        raise TypeError(f"Object of type {type(o)} is not JSON serializable")

    def dumps(obj: Any, **kwargs: Any) -> str:
        return json.dumps(obj, default=_default, **kwargs)

    def loads(s: str | bytes) -> Any:
        return json.loads(s)
