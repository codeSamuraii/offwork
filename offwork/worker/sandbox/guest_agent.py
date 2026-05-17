#!/usr/bin/env python3
"""Lightweight guest agent for offwork sandbox.

This script is deployed inside the Docker container and listens for
execution requests over TCP.  It is completely self-contained (stdlib
only) so the container only needs a working Python ≥ 3.10 interpreter.

Wire protocol
-------------
Length-prefixed JSON (4-byte big-endian header + UTF-8 JSON payload),
identical to ``offwork.worker.sandbox._protocol``.

Request format::

    {
        "source":        "<reconstructed Python source>",
        "function_name": "f",
        "args":          [21],
        "kwargs":        {},
        "owner_class":   null
    }

Success response::

    {"status": "ok", "result": <value>}

Error response::

    {
        "status":          "error",
        "error_type":      "ValueError",
        "error_message":   "...",
        "error_traceback": "..."
    }

Usage::

    python guest_agent.py [--host 0.0.0.0] [--port 9749]
"""

import sys
import json
import enum
import uuid
import types
import struct
import base64
import pickle
import asyncio
import inspect
import pathlib
import argparse
import functools
import ipaddress
import collections
import traceback as tb_mod
import contextvars
import datetime as _dt
from decimal import Decimal
from fractions import Fraction
from typing import Any

# ---------------------------------------------------------------------------
# Wire helpers (duplicated from _protocol.py to stay dependency-free)
# ---------------------------------------------------------------------------

_HEADER = struct.Struct("!I")

_OBJECT_SENTINEL = "__offwork_obj__"
_BYTES_SENTINEL = "__offwork_bytes__"
_BUILTIN_SENTINEL = "__offwork_builtin__"
_TUPLE_SENTINEL = "__offwork_tuple__"
_DICT_SENTINEL = "__offwork_dict__"
_PICKLE_SENTINEL = "__offwork_pickle__"


def _encode(obj: dict[str, Any]) -> bytes:
    payload = json.dumps(obj, separators=(",", ":"), default=_json_default).encode()
    return _HEADER.pack(len(payload)) + payload


def _json_default(o: Any) -> Any:
    """Fallback for objects the JSON encoder doesn't natively handle.

    The host-side encoder pre-walks the tree, but the guest receives
    *real* objects from user code (return values, exceptions) that need
    the same sentinel treatment on the way back.
    """
    return _to_jsonable(o)


async def _recv(reader: asyncio.StreamReader) -> dict[str, Any]:
    raw = await reader.readexactly(_HEADER.size)
    (length,) = _HEADER.unpack(raw)
    data = await reader.readexactly(length)
    result: dict[str, Any] = json.loads(data)
    return result


async def _send(writer: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
    writer.write(_encode(obj))
    await writer.drain()


# ---------------------------------------------------------------------------
# Sentinel encoding / decoding (mirrors offwork.core.task)
# ---------------------------------------------------------------------------

_FACTORY_BY_NAME: dict[str, Any] = {
    "int": int, "list": list, "dict": dict, "set": set,
    "tuple": tuple, "frozenset": frozenset, "str": str, "float": float,
    "bytes": bytes, "bool": bool,
}

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


def _encode_factory(factory: Any) -> str | None:
    if factory is None:
        return None
    name = getattr(factory, "__name__", None)
    if isinstance(name, str) and _FACTORY_BY_NAME.get(name) is factory:
        return name
    return None


def _encode_builtin(o: object) -> dict[str, Any] | None:
    if isinstance(o, enum.Enum):
        return {
            "type": "enum",
            "cls": type(o).__name__,
            "name": o.name,
            "value": _to_jsonable(o.value),
        }
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


def _extract_object_state(o: object) -> dict[str, Any] | None:
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
    try:
        data = pickle.dumps(o)
    except Exception as exc:
        raise TypeError(
            f"Object of type {type(o).__name__} is not serializable: {exc}"
        ) from exc
    return {_PICKLE_SENTINEL: base64.b64encode(data).decode("ascii")}


def _decode_builtin(info: dict[str, Any], namespace: dict[str, Any]) -> Any:
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


def _reconstruct_object(info: dict[str, Any], namespace: dict[str, Any]) -> Any:
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


# ---------------------------------------------------------------------------
# Execution engine
# ---------------------------------------------------------------------------


def _extract_callable(
    namespace: dict[str, Any],
    function_name: str,
    owner_class: str | None,
) -> Any:
    if owner_class:
        class_name = owner_class.rsplit(".", 1)[-1]
        cls = namespace.get(class_name)
        if cls is None:
            raise RuntimeError(f"Class '{class_name}' not found")
        func = getattr(cls, function_name, None)
        if func is None:
            raise RuntimeError(
                f"Method '{function_name}' not found on '{class_name}'"
            )
        return func
    func = namespace.get(function_name)
    if func is None:
        raise RuntimeError(f"Function '{function_name}' not found")
    return func


def _install_offwork_shim(
    writer: asyncio.StreamWriter | None,
) -> tuple[Any, ...]:
    """Install a fake ``offwork`` package so ``from offwork import progress`` works.

    The shim's ``progress()`` writes a ``{"status": "progress", ...}``
    frame directly to *writer*.  When *writer* is ``None`` (unit tests),
    progress calls are silently ignored.

    Returns the previous ``sys.modules`` entries so they can be restored.
    """

    def _progress(
        _value: float,
        _total: int | None = None,
        /,
        *,
        message: str | None = None,
    ) -> None:
        if writer is None:
            return
        msg: dict[str, Any] = {"status": "progress", "current": _value}
        if _total is not None:
            msg["total"] = _total
        if message is not None:
            msg["message"] = message
        # Synchronous write — fine from the event-loop thread and from
        # executor threads via loop.call_soon_threadsafe (see below).
        writer.write(_encode(msg))

    # Build a minimal offwork package hierarchy.
    fake = types.ModuleType("offwork")
    fake.progress = _progress  # type: ignore[attr-defined]
    fake_core = types.ModuleType("offwork.core")
    fake_core_progress = types.ModuleType("offwork.core.progress")
    fake_core_progress.progress = _progress  # type: ignore[attr-defined]
    fake.core = fake_core  # type: ignore[attr-defined]
    fake_core.progress = fake_core_progress  # type: ignore[attr-defined]

    saved = (
        sys.modules.get("offwork"),
        sys.modules.get("offwork.core"),
        sys.modules.get("offwork.core.progress"),
    )
    sys.modules["offwork"] = fake
    sys.modules["offwork.core"] = fake_core
    sys.modules["offwork.core.progress"] = fake_core_progress
    return saved


def _uninstall_offwork_shim(saved: tuple[Any, ...]) -> None:
    for key, prev in zip(
        ("offwork", "offwork.core", "offwork.core.progress"), saved
    ):
        if prev is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = prev


async def _execute_request(
    req: dict[str, Any],
    writer: asyncio.StreamWriter | None = None,
) -> dict[str, Any]:
    """Execute a single request and return a response dict.

    When *writer* is provided, ``offwork.progress()`` calls inside the
    user function are forwarded as ``{"status": "progress", ...}``
    frames over the wire before the final ``ok`` / ``error`` response.
    """
    saved = _install_offwork_shim(writer)
    try:
        source: str = req["source"]
        function_name: str = req["function_name"]
        raw_args: list[Any] = req.get("args", [])
        raw_kwargs: dict[str, Any] = req.get("kwargs", {})
        owner_class: str | None = req.get("owner_class")

        # Compile and exec
        code = compile(source, f"<offwork-sandbox:{function_name}>", "exec")
        namespace: dict[str, Any] = {}
        exec(code, namespace)  # noqa: S102

        # Resolve serialised object arguments
        args = tuple(_resolve(a, namespace) for a in raw_args)
        kwargs = {k: _resolve(v, namespace) for k, v in raw_kwargs.items()}

        func = _extract_callable(namespace, function_name, owner_class)

        if inspect.iscoroutinefunction(func):
            result = await func(*args, **kwargs)
        else:
            # Run sync functions in an executor so the event loop stays
            # free to flush buffered progress writes.
            loop = asyncio.get_running_loop()
            ctx = contextvars.copy_context()
            result = await loop.run_in_executor(
                None, ctx.run, functools.partial(func, *args, **kwargs),
            )

        # Flush any buffered progress frames before the final response.
        if writer is not None:
            await writer.drain()

        return {"status": "ok", "result": _to_jsonable(result)}

    except Exception as exc:
        return {
            "status": "error",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "error_traceback": "".join(tb_mod.format_exception(exc)),
        }
    finally:
        _uninstall_offwork_shim(saved)


# ---------------------------------------------------------------------------
# TCP server
# ---------------------------------------------------------------------------


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    peer = writer.get_extra_info("peername")
    print(f"[guest-agent] connection from {peer}", flush=True)
    try:
        while True:
            req = await _recv(reader)
            # Cheap liveness handshake used by the host to confirm the
            # in-container agent is actually accepting requests (a TCP
            # connection alone isn't sufficient: on Linux docker-proxy
            # accepts the connection on the host port even before the
            # guest agent process has started listening).
            if req.get("op") == "ping":
                await _send(writer, {"status": "pong"})
                continue
            resp = await _execute_request(req, writer)
            await _send(writer, resp)
    except (asyncio.IncompleteReadError, ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def serve(host: str, port: int) -> None:
    server = await asyncio.start_server(_handle_client, host, port)
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    print(f"[guest-agent] listening on {addrs}", flush=True)
    async with server:
        await server.serve_forever()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="offwork sandbox guest agent")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--port", type=int, default=9749, help="Bind port")
    args = parser.parse_args()

    print(
        f"[guest-agent] starting on {args.host}:{args.port} "
        f"(Python {sys.version})",
        flush=True,
    )
    try:
        asyncio.run(serve(args.host, args.port))
    except KeyboardInterrupt:
        print("[guest-agent] shutting down", flush=True)


if __name__ == "__main__":
    main()
