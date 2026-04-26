"""Task dataclass: serializable envelope bundling a graph with arguments."""

import base64
import datetime as _dt
import json
import pathlib
import uuid
from decimal import Decimal
from typing import Any, Self
from dataclasses import field, dataclass

from pyfuse.core.errors import SignatureError
from pyfuse.core.signing import verify_signature, compute_signature

_OBJECT_SENTINEL = "__pyfuse_obj__"
_BYTES_SENTINEL = "__pyfuse_bytes__"
_BUILTIN_SENTINEL = "__pyfuse_builtin__"


def _encode_builtin(o: object) -> dict[str, Any] | None:
    """Encode common stdlib types to a JSON-safe sentinel.

    Returns ``None`` if *o* is not a recognised builtin -- the caller
    falls back to object-state serialization.
    """
    # datetime is a subclass of date, so check it first.
    if isinstance(o, _dt.datetime):
        return {"type": "datetime", "value": o.isoformat()}
    if isinstance(o, _dt.date):
        return {"type": "date", "value": o.isoformat()}
    if isinstance(o, _dt.time):
        return {"type": "time", "value": o.isoformat()}
    if isinstance(o, _dt.timedelta):
        return {"type": "timedelta", "value": o.total_seconds()}
    if isinstance(o, Decimal):
        return {"type": "decimal", "value": str(o)}
    if isinstance(o, uuid.UUID):
        return {"type": "uuid", "value": o.hex}
    if isinstance(o, complex):
        return {"type": "complex", "value": [o.real, o.imag]}
    if isinstance(o, (set, frozenset)):
        kind = "frozenset" if isinstance(o, frozenset) else "set"
        return {"type": kind, "value": list(o)}
    if isinstance(o, pathlib.PurePath):
        return {"type": "path", "value": str(o), "cls": type(o).__name__}
    return None


_PATH_CLASSES: dict[str, type[pathlib.PurePath]] = {
    "PurePath": pathlib.PurePath,
    "PurePosixPath": pathlib.PurePosixPath,
    "PureWindowsPath": pathlib.PureWindowsPath,
    "Path": pathlib.Path,
    "PosixPath": pathlib.PurePosixPath,
    "WindowsPath": pathlib.PureWindowsPath,
}


def _decode_builtin(info: dict[str, Any], namespace: dict[str, Any]) -> Any:
    """Reverse :func:`_encode_builtin`."""
    kind = info.get("type")
    raw = info.get("value")
    if kind == "datetime":
        return _dt.datetime.fromisoformat(raw)
    if kind == "date":
        return _dt.date.fromisoformat(raw)
    if kind == "time":
        return _dt.time.fromisoformat(raw)
    if kind == "timedelta":
        return _dt.timedelta(seconds=raw)
    if kind == "decimal":
        return Decimal(raw)
    if kind == "uuid":
        return uuid.UUID(hex=raw)
    if kind == "complex":
        return complex(raw[0], raw[1])
    if kind == "set":
        return {_resolve(v, namespace) for v in raw}
    if kind == "frozenset":
        return frozenset(_resolve(v, namespace) for v in raw)
    if kind == "path":
        # Try to honour the original class; fall back to a sensible
        # OS-portable default if the concrete subclass cannot be
        # instantiated on this platform.
        cls = _PATH_CLASSES.get(info.get("cls", ""), pathlib.PurePath)
        try:
            return cls(raw)
        except (NotImplementedError, TypeError):
            return pathlib.PurePath(raw)
    raise ValueError(f"Unknown builtin sentinel type: {kind!r}")


class _TaskEncoder(json.JSONEncoder):
    """JSON encoder that serializes arbitrary objects via class name + __dict__."""

    def default(self, o: object) -> Any:
        if isinstance(o, (bytes, bytearray)):
            return {
                _BYTES_SENTINEL: {
                    "data": base64.b64encode(bytes(o)).decode("ascii"),
                    "type": type(o).__name__,
                }
            }
        builtin = _encode_builtin(o)
        if builtin is not None:
            return {_BUILTIN_SENTINEL: builtin}
        if hasattr(o, "__dict__"):
            state = o.__dict__
        elif hasattr(type(o), "__slots__"):
            all_slots: set[str] = set()
            for klass in type(o).__mro__:
                all_slots.update(getattr(klass, "__slots__", ()))
            all_slots -= {"__weakref__", "__dict__"}
            state = {
                slot: getattr(o, slot)
                for slot in sorted(all_slots)
                if hasattr(o, slot)
            }
        else:
            raise TypeError(
                f"Object of type {type(o).__name__} is not JSON serializable"
            )
        return {
            _OBJECT_SENTINEL: {
                "class": type(o).__name__,
                "state": state,
            }
        }


def _reconstruct_object(info: dict[str, Any], namespace: dict[str, Any]) -> Any:
    """Rebuild a single serialized object from its sentinel payload."""
    cls = namespace.get(info["class"])
    if cls is None:
        return {_OBJECT_SENTINEL: info}
    obj = cls.__new__(cls)
    state = {k: _resolve(v, namespace) for k, v in info.get("state", {}).items()}
    if hasattr(obj, "__dict__"):
        obj.__dict__.update(state)
    else:
        for key, val in state.items():
            object.__setattr__(obj, key, val)
    return obj


def _resolve(value: Any, namespace: dict[str, Any]) -> Any:
    """Recursively resolve serialized object sentinels using *namespace*."""
    if isinstance(value, list):
        return [_resolve(v, namespace) for v in value]
    if not isinstance(value, dict):
        return value
    if len(value) == 1 and _OBJECT_SENTINEL in value:
        return _reconstruct_object(value[_OBJECT_SENTINEL], namespace)
    if len(value) == 1 and _BYTES_SENTINEL in value:
        info = value[_BYTES_SENTINEL]
        raw = base64.b64decode(info["data"])
        return bytearray(raw) if info.get("type") == "bytearray" else raw
    if len(value) == 1 and _BUILTIN_SENTINEL in value:
        return _decode_builtin(value[_BUILTIN_SENTINEL], namespace)
    return {k: _resolve(v, namespace) for k, v in value.items()}


def resolve_args(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    namespace: dict[str, Any],
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Resolve serialized object sentinels in task arguments.

    Called by the worker after reconstructing the function's namespace,
    so that class instances passed as arguments can be rebuilt.
    """
    return (
        tuple(_resolve(a, namespace) for a in args),
        {k: _resolve(v, namespace) for k, v in kwargs.items()},
    )


@dataclass(frozen=True)
class Task:
    """A serializable envelope for remote function execution.

    Bundles the serialized dependency graph with the target function
    name and its arguments, so the consumer side needs zero knowledge
    of pyfuse internals to dispatch work.

    When a shared key is provided (via :meth:`to_signed_json`), the
    serialized payload carries an HMAC-SHA256 signature that the worker
    can verify before execution.
    """

    graph_json: str
    function_name: str
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timeout: float | None = None
    retries: int = 0
    retry_delay: float = 1.0
    scheduled_at: float | None = None
    recur_interval: float | None = None
    schedule_id: str | None = None
    throttle: float | None = None
    signature: str | None = None

    # -- Serialization -------------------------------------------------------

    def _to_dict(self) -> dict[str, Any]:
        """Build the core payload dict (without signature)."""
        d: dict[str, Any] = {
            "id": self.task_id,
            "graph": self.graph_json,
            "function": self.function_name,
            "args": list(self.args),
            "kwargs": self.kwargs,
        }
        if self.timeout is not None:
            d["timeout"] = self.timeout
        if self.retries:
            d["retries"] = self.retries
        if self.retry_delay != 1.0:
            d["retry_delay"] = self.retry_delay
        if self.scheduled_at is not None:
            d["scheduled_at"] = self.scheduled_at
        if self.recur_interval is not None:
            d["recur_interval"] = self.recur_interval
        if self.schedule_id is not None:
            d["schedule_id"] = self.schedule_id
        if self.throttle is not None:
            d["throttle"] = self.throttle
        return d

    def to_json(self, *, signing_key: bytes | None = None) -> str:
        """Serialize the task envelope to a JSON string.

        Parameters
        ----------
        signing_key
            When provided, the payload is HMAC-SHA256 signed and the
            signature is included in the envelope.  Workers that hold
            the same key can verify it with :meth:`from_signed_json`.
        """
        d = self._to_dict()
        if signing_key is not None:
            payload = json.dumps(d, cls=_TaskEncoder, separators=(",", ":"), sort_keys=True)
            d["signature"] = compute_signature(payload, signing_key)
        return json.dumps(d, cls=_TaskEncoder)

    @classmethod
    def from_json(
        cls,
        json_str: str | bytes,
        *,
        signing_key: bytes | None = None,
    ) -> Self:
        """Deserialize a task envelope from a JSON string.

        Parameters
        ----------
        signing_key
            When provided **and** the envelope contains a ``signature``
            field, the signature is verified.  If verification fails,
            :class:`~pyfuse.core.errors.SignatureError` is raised.
            Unsigned tasks are accepted when *signing_key* is ``None``.

        Raises
        ------
        SignatureError
            If the signature is present but invalid, or if *signing_key*
            is provided but the envelope has no signature.
        """
        data = json.loads(json_str)
        sig = data.pop("signature", None)

        if signing_key is not None:
            if not sig:
                raise SignatureError(
                    "Task is unsigned but signing is enabled — "
                    "rejecting unauthenticated task"
                )
            # Re-serialize without signature for verification
            payload = json.dumps(data, cls=_TaskEncoder, separators=(",", ":"), sort_keys=True)
            if not verify_signature(payload, sig, signing_key):
                raise SignatureError("Task signature verification failed")

        return cls(
            graph_json=data["graph"],
            function_name=data["function"],
            args=tuple(data.get("args", ())),
            kwargs=data.get("kwargs", {}),
            task_id=data.get("id", uuid.uuid4().hex[:12]),
            timeout=data.get("timeout"),
            retries=data.get("retries", 0),
            retry_delay=data.get("retry_delay", 1.0),
            scheduled_at=data.get("scheduled_at"),
            recur_interval=data.get("recur_interval"),
            schedule_id=data.get("schedule_id"),
            throttle=data.get("throttle"),
            signature=sig or None,  # normalise empty string to None
        )
