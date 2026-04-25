#!/usr/bin/env python3
"""Lightweight guest agent for pyfuse sandbox.

This script is deployed inside the Docker container and listens for
execution requests over TCP.  It is completely self-contained (stdlib
only) so the container only needs a working Python ≥ 3.10 interpreter.

Wire protocol
-------------
Length-prefixed JSON (4-byte big-endian header + UTF-8 JSON payload),
identical to ``pyfuse.worker.sandbox._protocol``.

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
import types
import struct
import asyncio
import inspect
import argparse
import functools
import traceback as tb_mod
import contextvars
from typing import Any

# ---------------------------------------------------------------------------
# Wire helpers (duplicated from _protocol.py to stay dependency-free)
# ---------------------------------------------------------------------------

_HEADER = struct.Struct("!I")

_OBJECT_SENTINEL = "__pyfuse_obj__"


def _encode(obj: dict[str, Any]) -> bytes:
    payload = json.dumps(obj, separators=(",", ":")).encode()
    return _HEADER.pack(len(payload)) + payload


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
# Object resolution (mirrors pyfuse.core.task._resolve)
# ---------------------------------------------------------------------------


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
    if len(value) == 1 and _OBJECT_SENTINEL in value:
        return _reconstruct_object(value[_OBJECT_SENTINEL], namespace)
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


def _install_pyfuse_shim(
    writer: asyncio.StreamWriter | None,
) -> tuple[Any, ...]:
    """Install a fake ``pyfuse`` package so ``from pyfuse import progress`` works.

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

    # Build a minimal pyfuse package hierarchy.
    fake = types.ModuleType("pyfuse")
    fake.progress = _progress  # type: ignore[attr-defined]
    fake_core = types.ModuleType("pyfuse.core")
    fake_core_progress = types.ModuleType("pyfuse.core.progress")
    fake_core_progress.progress = _progress  # type: ignore[attr-defined]
    fake.core = fake_core  # type: ignore[attr-defined]
    fake_core.progress = fake_core_progress  # type: ignore[attr-defined]

    saved = (
        sys.modules.get("pyfuse"),
        sys.modules.get("pyfuse.core"),
        sys.modules.get("pyfuse.core.progress"),
    )
    sys.modules["pyfuse"] = fake
    sys.modules["pyfuse.core"] = fake_core
    sys.modules["pyfuse.core.progress"] = fake_core_progress
    return saved


def _uninstall_pyfuse_shim(saved: tuple[Any, ...]) -> None:
    for key, prev in zip(
        ("pyfuse", "pyfuse.core", "pyfuse.core.progress"), saved
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

    When *writer* is provided, ``pyfuse.progress()`` calls inside the
    user function are forwarded as ``{"status": "progress", ...}``
    frames over the wire before the final ``ok`` / ``error`` response.
    """
    saved = _install_pyfuse_shim(writer)
    try:
        source: str = req["source"]
        function_name: str = req["function_name"]
        raw_args: list[Any] = req.get("args", [])
        raw_kwargs: dict[str, Any] = req.get("kwargs", {})
        owner_class: str | None = req.get("owner_class")

        # Compile and exec
        code = compile(source, f"<pyfuse-sandbox:{function_name}>", "exec")
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

        return {"status": "ok", "result": result}

    except Exception as exc:
        return {
            "status": "error",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "error_traceback": "".join(tb_mod.format_exception(exc)),
        }
    finally:
        _uninstall_pyfuse_shim(saved)


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
    parser = argparse.ArgumentParser(description="pyfuse sandbox guest agent")
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
