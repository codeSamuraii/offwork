"""Fast JSON helpers — uses *orjson* when available.

``orjson`` is a high-performance JSON library written in Rust that is
typically 3–10× faster than the standard-library :mod:`json` module.
Since pyfuse serializes/deserializes JSON on every task submission and
execution, faster JSON can measurably reduce end-to-end latency.

Install it with::

    pip install pyfuse[fast]   # or: pip install orjson

If *orjson* is not installed the helpers fall back transparently to
:func:`json.dumps` / :func:`json.loads` with compact separators.
"""

import json as _json
from typing import Any

try:
    import orjson as _orjson
except ImportError:  # pragma: no cover
    _orjson = None  # type: ignore[assignment]

_has_orjson: bool = _orjson is not None


def dumps(obj: Any) -> str:
    """Serialize *obj* to a compact JSON string."""
    if _has_orjson:
        return _orjson.dumps(obj).decode()  # type: ignore[union-attr]
    return _json.dumps(obj, separators=(",", ":"))


def loads(data: str | bytes) -> Any:
    """Deserialize a JSON string or bytes to a Python object."""
    if _has_orjson:
        return _orjson.loads(data)  # type: ignore[union-attr]
    return _json.loads(data)
