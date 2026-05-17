"""Task dataclass: serializable envelope bundling a graph with arguments."""

import base64
import collections
import datetime as _dt
import enum
import ipaddress
import json
import pathlib
import pickle
import re
import uuid
from decimal import Decimal
from fractions import Fraction
from typing import Any, Self
from dataclasses import field, dataclass

from offwork.core.errors import SignatureError
from offwork.core.signing import verify_signature, compute_signature

_OBJECT_SENTINEL = "__offwork_obj__"
_BYTES_SENTINEL = "__offwork_bytes__"
_BUILTIN_SENTINEL = "__offwork_builtin__"
_TUPLE_SENTINEL = "__offwork_tuple__"
_DICT_SENTINEL = "__offwork_dict__"
_PICKLE_SENTINEL = "__offwork_pickle__"


_FACTORY_BY_NAME: dict[str, Any] = {
    "int": int, "list": list, "dict": dict, "set": set,
    "tuple": tuple, "frozenset": frozenset, "str": str, "float": float,
    "bytes": bytes, "bool": bool,
}


def _encode_factory(factory: Any) -> str | None:
    """Encode a defaultdict factory if it's a recognised builtin, else None."""
    if factory is None:
        return None
    name = getattr(factory, "__name__", None)
    if isinstance(name, str) and _FACTORY_BY_NAME.get(name) is factory:
        return name
    return None


def _encode_builtin(o: object) -> dict[str, Any] | None:
    """Encode common stdlib types to a JSON-safe sentinel payload.

    Returns ``None`` if *o* is not a recognised builtin -- the caller
    falls back to object-state serialization or pickle.
    """
    # IntEnum / StrEnum subclass int / str, so check enum first.
    if isinstance(o, enum.Enum):
        return {
            "type": "enum",
            "cls": type(o).__name__,
            "name": o.name,
            "value": _to_jsonable(o.value),
        }
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
    if isinstance(o, Fraction):
        return {"type": "fraction", "value": [o.numerator, o.denominator]}
    if isinstance(o, uuid.UUID):
        return {"type": "uuid", "value": o.hex}
    if isinstance(o, complex):
        return {"type": "complex", "value": [o.real, o.imag]}
    if isinstance(o, range):
        return {"type": "range", "value": [o.start, o.stop, o.step]}
    if isinstance(o, frozenset):
        return {"type": "frozenset", "value": [_to_jsonable(v) for v in o]}
    if isinstance(o, set):
        return {"type": "set", "value": [_to_jsonable(v) for v in o]}
    if isinstance(o, collections.deque):
        return {
            "type": "deque",
            "value": [_to_jsonable(v) for v in o],
            "maxlen": o.maxlen,
        }
    if isinstance(o, pathlib.PurePath):
        return {"type": "path", "value": str(o), "cls": type(o).__name__}
    if isinstance(
        o,
        (
            ipaddress.IPv4Address, ipaddress.IPv6Address,
            ipaddress.IPv4Network, ipaddress.IPv6Network,
            ipaddress.IPv4Interface, ipaddress.IPv6Interface,
        ),
    ):
        return {"type": "ipaddress", "cls": type(o).__name__, "value": str(o)}
    return None


_PATH_CLASSES: dict[str, type[pathlib.PurePath]] = {
    "PurePath": pathlib.PurePath,
    "PurePosixPath": pathlib.PurePosixPath,
    "PureWindowsPath": pathlib.PureWindowsPath,
    "Path": pathlib.Path,
    "PosixPath": pathlib.PurePosixPath,
    "WindowsPath": pathlib.PureWindowsPath,
}

_IP_CLASSES: dict[str, Any] = {
    "IPv4Address": ipaddress.IPv4Address,
    "IPv6Address": ipaddress.IPv6Address,
    "IPv4Network": ipaddress.IPv4Network,
    "IPv6Network": ipaddress.IPv6Network,
    "IPv4Interface": ipaddress.IPv4Interface,
    "IPv6Interface": ipaddress.IPv6Interface,
}


def _decode_builtin(info: dict[str, Any], namespace: dict[str, Any]) -> Any:
    """Reverse :func:`_encode_builtin`."""
    kind = info.get("type")
    raw: Any = info.get("value")
    if kind == "datetime":
        return _dt.datetime.fromisoformat(str(raw))
    if kind == "date":
        return _dt.date.fromisoformat(str(raw))
    if kind == "time":
        return _dt.time.fromisoformat(str(raw))
    if kind == "timedelta":
        return _dt.timedelta(seconds=float(raw))
    if kind == "decimal":
        return Decimal(str(raw))
    if kind == "fraction":
        return Fraction(int(raw[0]), int(raw[1]))
    if kind == "uuid":
        return uuid.UUID(hex=str(raw))
    if kind == "complex":
        return complex(raw[0], raw[1])
    if kind == "range":
        return range(raw[0], raw[1], raw[2])
    if kind == "set":
        return {_resolve(v, namespace) for v in raw}
    if kind == "frozenset":
        return frozenset(_resolve(v, namespace) for v in raw)
    if kind == "deque":
        return collections.deque(
            (_resolve(v, namespace) for v in raw),
            maxlen=info.get("maxlen"),
        )
    if kind == "counter":
        return collections.Counter({
            _resolve(k, namespace): v for k, v in info["items"]
        })
    if kind == "ordereddict":
        return collections.OrderedDict(
            (_resolve(k, namespace), _resolve(v, namespace))
            for k, v in info["items"]
        )
    if kind == "defaultdict":
        factory = _FACTORY_BY_NAME.get(info.get("factory") or "")
        dd: collections.defaultdict[Any, Any] = collections.defaultdict(factory)
        for k, v in info["items"]:
            dd[_resolve(k, namespace)] = _resolve(v, namespace)
        return dd
    if kind == "namedtuple":
        cls = namespace.get(info["cls"])
        values = [_resolve(v, namespace) for v in info["values"]]
        if cls is None:
            return tuple(values)
        return cls(*values)
    if kind == "enum":
        cls = namespace.get(info["cls"])
        if cls is None:
            return _resolve(raw, namespace)
        try:
            return cls[info["name"]]
        except KeyError:
            return cls(_resolve(raw, namespace))
    if kind == "path":
        # Try to honour the original class; fall back to a sensible
        # OS-portable default if the concrete subclass cannot be
        # instantiated on this platform.
        cls = _PATH_CLASSES.get(info.get("cls", ""), pathlib.PurePath)
        try:
            return cls(str(raw))
        except (NotImplementedError, TypeError):
            return pathlib.PurePath(str(raw))
    if kind == "ipaddress":
        ip_cls = _IP_CLASSES.get(info.get("cls", ""))
        if ip_cls is None:
            return str(raw)
        return ip_cls(str(raw))
    raise ValueError(f"Unknown builtin sentinel type: {kind!r}")


def _extract_object_state(o: object) -> dict[str, Any] | None:
    """Return the per-instance state dict, or ``None`` if not extractable."""
    if hasattr(o, "__dict__"):
        d = getattr(o, "__dict__", None)
        if isinstance(d, dict):
            return dict(d)
    if hasattr(type(o), "__slots__"):
        all_slots: set[str] = set()
        for klass in type(o).__mro__:
            all_slots.update(getattr(klass, "__slots__", ()))
        all_slots -= {"__weakref__", "__dict__"}
        return {
            slot: getattr(o, slot)
            for slot in sorted(all_slots)
            if hasattr(o, slot)
        }
    return None


def _to_jsonable(o: Any) -> Any:
    """Recursively convert *o* to a JSON-safe value using sentinels.

    Order of checks matters: ``bool`` and ``IntEnum`` subclass ``int``;
    ``Counter``/``OrderedDict``/``defaultdict`` subclass ``dict``;
    ``NamedTuple`` subclasses ``tuple``.
    """
    # Primitives. None / bool / str pass through; bool must come before int
    # but JSON treats True/False natively so isinstance(_, int) is harmless
    # *after* the enum check.
    if o is None or isinstance(o, (str, bool)):
        return o
    if isinstance(o, enum.Enum):
        return {_BUILTIN_SENTINEL: _encode_builtin(o)}
    if isinstance(o, (int, float)):
        return o
    if isinstance(o, (bytes, bytearray)):
        return {
            _BYTES_SENTINEL: {
                "data": base64.b64encode(bytes(o)).decode("ascii"),
                "type": type(o).__name__,
            }
        }
    if isinstance(o, memoryview):
        return {
            _BYTES_SENTINEL: {
                "data": base64.b64encode(bytes(o)).decode("ascii"),
                "type": "memoryview",
            }
        }
    # NamedTuple before tuple (NamedTuple subclasses tuple).
    if isinstance(o, tuple):
        if hasattr(o, "_fields") and hasattr(o, "_asdict"):
            return {
                _BUILTIN_SENTINEL: {
                    "type": "namedtuple",
                    "cls": type(o).__name__,
                    "fields": list(o._fields),
                    "values": [_to_jsonable(v) for v in o],
                }
            }
        return {_TUPLE_SENTINEL: [_to_jsonable(v) for v in o]}
    if isinstance(o, list):
        return [_to_jsonable(v) for v in o]
    if isinstance(o, dict):
        # dict subclasses (Counter, OrderedDict, defaultdict) first.
        if isinstance(o, collections.Counter):
            return {
                _BUILTIN_SENTINEL: {
                    "type": "counter",
                    "items": [[_to_jsonable(k), v] for k, v in o.items()],
                }
            }
        if isinstance(o, collections.OrderedDict):
            return {
                _BUILTIN_SENTINEL: {
                    "type": "ordereddict",
                    "items": [
                        [_to_jsonable(k), _to_jsonable(v)] for k, v in o.items()
                    ],
                }
            }
        if isinstance(o, collections.defaultdict):
            return {
                _BUILTIN_SENTINEL: {
                    "type": "defaultdict",
                    "factory": _encode_factory(o.default_factory),
                    "items": [
                        [_to_jsonable(k), _to_jsonable(v)] for k, v in o.items()
                    ],
                }
            }
        if all(isinstance(k, str) for k in o):
            return {k: _to_jsonable(v) for k, v in o.items()}
        return {
            _DICT_SENTINEL: [
                [_to_jsonable(k), _to_jsonable(v)] for k, v in o.items()
            ]
        }
    builtin = _encode_builtin(o)
    if builtin is not None:
        return {_BUILTIN_SENTINEL: builtin}
    state = _extract_object_state(o)
    if state is not None:
        return {
            _OBJECT_SENTINEL: {
                "class": type(o).__name__,
                "state": {k: _to_jsonable(v) for k, v in state.items()},
            }
        }
    # Last-resort: pickle. The task envelope is HMAC-signed end-to-end
    # so unpickling on the worker is no more dangerous than the existing
    # ``exec`` of reconstructed source.
    try:
        data = pickle.dumps(o)
    except Exception as exc:
        raise TypeError(
            f"Object of type {type(o).__name__} is not serializable: {exc}"
        ) from exc
    return {_PICKLE_SENTINEL: base64.b64encode(data).decode("ascii")}


class _TaskEncoder(json.JSONEncoder):
    """JSON encoder that pre-walks the tree to apply offwork sentinels.

    JSON's native handling of ``tuple``/``dict``/``list`` would bypass
    sentinels, so :meth:`iterencode` preprocesses the full tree via
    :func:`_to_jsonable` before delegating to the base encoder.
    Both :meth:`encode` and :meth:`iterencode` route through here.
    """

    def iterencode(self, o: Any, _one_shot: bool = False) -> Any:
        return super().iterencode(_to_jsonable(o), _one_shot)

    def default(self, o: object) -> Any:  # pragma: no cover - unreachable
        return _to_jsonable(o)


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
    if len(value) == 1:
        if _OBJECT_SENTINEL in value:
            return _reconstruct_object(value[_OBJECT_SENTINEL], namespace)
        if _BYTES_SENTINEL in value:
            info = value[_BYTES_SENTINEL]
            raw = base64.b64decode(info["data"])
            kind = info.get("type")
            if kind == "bytearray":
                return bytearray(raw)
            if kind == "memoryview":
                return memoryview(raw)
            return raw
        if _BUILTIN_SENTINEL in value:
            return _decode_builtin(value[_BUILTIN_SENTINEL], namespace)
        if _TUPLE_SENTINEL in value:
            return tuple(_resolve(v, namespace) for v in value[_TUPLE_SENTINEL])
        if _DICT_SENTINEL in value:
            return {
                _resolve(k, namespace): _resolve(v, namespace)
                for k, v in value[_DICT_SENTINEL]
            }
        if _PICKLE_SENTINEL in value:
            return pickle.loads(base64.b64decode(value[_PICKLE_SENTINEL]))
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
    of offwork internals to dispatch work.

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
            :class:`~offwork.core.errors.SignatureError` is raised.
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
