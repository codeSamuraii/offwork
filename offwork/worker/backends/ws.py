"""WebSocket backend for hosted offwork brokers.

One persistent WS connection per process, multiplexed request/response by
``request_id``. Reconnects automatically with bounded backoff. The wire
protocol is documented in ``cloud_poc/docs/WS_PLAN.md`` and implemented
on the server side in ``backend/app/routes/broker_ws.py``.

Notes
-----

- ``websockets`` (PyPI) is the only runtime dependency. Imported lazily
  so a missing extras install only fails when this backend is actually
  used.
- ``connect()`` is idempotent and serialised on a single lock — many
  concurrent calls will share one in-flight connect attempt.
- Reconnect strategy is conservative: mutating ops that were in flight
  when the socket dropped surface as ``ConnectionError`` so the caller
  decides whether to retry. Idempotent reads (``try_get_result``, etc.)
  are safe to retry transparently but we still bubble the error to keep
  the rule simple.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

try:
    import websockets
except ImportError:
    raise ImportError(
        "websockets package is required for RabbitMQBackend. "
        "Install it with: pip install websockets"
    ) from None

from offwork.core.version import _VERSION
from offwork.worker.backends.base import Backend

logger = logging.getLogger(__name__)

_DEFAULT_BROKER_PATH = "/api/v1/broker/ws"
_DEFAULT_LONG_POLL_SECONDS = 30.0
_PROTOCOL_VERSION = 1
_RECONNECT_BACKOFF_MIN = 0.5
_RECONNECT_BACKOFF_MAX = 30.0
_HELLO_TIMEOUT = 10.0


class _Pending:
    __slots__ = ("future",)

    def __init__(self, future: asyncio.Future[dict[str, Any] | None]) -> None:
        self.future = future


class WebSocketBackend(Backend):
    """Persistent WebSocket transport for the hosted broker."""

    def __init__(self, url: str, *, role: str = "client") -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"ws", "wss"}:
            raise ValueError(f"Unsupported WS backend scheme: {parsed.scheme!r}")

        query_items = parse_qsl(parsed.query, keep_blank_values=True)
        api_key = ""
        filtered: list[tuple[str, str]] = []
        for key, value in query_items:
            if key == "api_key" and not api_key:
                api_key = value
                continue
            filtered.append((key, value))

        path = parsed.path or _DEFAULT_BROKER_PATH
        if not path.endswith("/ws"):
            path = path.rstrip("/") + "/ws"
        self._url = urlunparse(parsed._replace(path=path, query=urlencode(filtered)))
        self._api_key = api_key or None
        self._role = role

        self._lock = asyncio.Lock()
        self._ws: Any = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[str, _Pending] = {}
        self._closed = False

    # ------------------------------------------------------------------ #
    # Connection management
    # ------------------------------------------------------------------ #

    async def _connect(self) -> Any:
        ws = await websockets.connect(
            self._url,
            max_size=None,  # broker payloads (graph_json) can be large
            ping_interval=20.0,
            ping_timeout=20.0,
            open_timeout=10.0,
        )
        hello = {
            "type": "hello",
            "protocol": _PROTOCOL_VERSION,
            "role": self._role,
            "api_key": self._api_key,
            "agent": f"offwork/{_VERSION}",
        }
        await ws.send(json.dumps(hello))
        raw = await asyncio.wait_for(ws.recv(), timeout=_HELLO_TIMEOUT)
        frame = json.loads(raw)
        if frame.get("type") != "hello_ok" or frame.get("protocol") != _PROTOCOL_VERSION:
            await ws.close()
            raise ConnectionError(f"broker hello failed: {frame!r}")
        return ws

    async def _ensure_connected(self) -> Any:
        if self._closed:
            raise ConnectionError("WebSocketBackend is closed")
        if self._ws is not None:
            return self._ws
        async with self._lock:
            if self._ws is not None:
                return self._ws
            self._ws = await self._connect()
            self._reader_task = asyncio.create_task(self._read_loop(self._ws))
            return self._ws

    async def _read_loop(self, ws: Any) -> None:
        try:
            async for raw in ws:
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("broker WS: dropping non-JSON frame")
                    continue
                if frame.get("type") != "response":
                    continue
                req_id = frame.get("id")
                pending = self._pending.pop(req_id, None)
                if pending is None or pending.future.done():
                    continue
                if frame.get("ok"):
                    pending.future.set_result(frame.get("payload"))
                else:
                    err = frame.get("error") or {}
                    code = err.get("code", "error")
                    msg = err.get("message", "")
                    pending.future.set_exception(
                        RuntimeError(f"broker error [{code}]: {msg}")
                    )
        except Exception as exc:  # noqa: BLE001
            logger.debug("broker WS read loop ended: %s", exc)
        finally:
            await self._drop_connection(ws)

    async def _drop_connection(self, ws: Any) -> None:
        if self._ws is ws:
            self._ws = None
        # Fail every in-flight request — the caller decides whether to
        # retry (we don't replay mutating ops automatically).
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_exception(
                    ConnectionError("broker WS dropped")
                )
        self._pending.clear()
        try:
            await ws.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Request dispatch
    # ------------------------------------------------------------------ #

    async def _request(
        self,
        op: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any] | None:
        backoff = _RECONNECT_BACKOFF_MIN
        last_exc: Exception | None = None
        for attempt in range(3):
            req_id: str | None = None
            try:
                ws = await self._ensure_connected()
                req_id = uuid.uuid4().hex
                loop = asyncio.get_running_loop()
                future: asyncio.Future[dict[str, Any] | None] = loop.create_future()
                self._pending[req_id] = _Pending(future)
                frame = {
                    "type": "request",
                    "id": req_id,
                    "op": op,
                    "payload": payload or {},
                }
                await ws.send(json.dumps(frame))
                if timeout is None:
                    return await future
                return await asyncio.wait_for(future, timeout=timeout)
            except (ConnectionError, OSError) as exc:
                # Drop any orphaned pending entry before reconnecting so a
                # late response can't resolve a future no one awaits.
                if req_id is not None:
                    self._pending.pop(req_id, None)
                last_exc = exc
                if self._closed:
                    raise
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX)
                continue
            except asyncio.TimeoutError:
                # Drop the pending entry — the response, if it ever
                # arrives, has no waiter.
                if req_id is not None:
                    self._pending.pop(req_id, None)
                raise
        assert last_exc is not None
        raise last_exc

    # ------------------------------------------------------------------ #
    # Backend ABC
    # ------------------------------------------------------------------ #

    async def submit(self, task_json: str) -> None:
        await self._request("submit_task", {"task_json": task_json})

    async def listen(self) -> AsyncIterator[str]:
        while not self._closed:
            try:
                body = await self._request(
                    "claim_task",
                    {"wait_seconds": _DEFAULT_LONG_POLL_SECONDS},
                    timeout=_DEFAULT_LONG_POLL_SECONDS + 10.0,
                )
            except (ConnectionError, OSError):
                await asyncio.sleep(_RECONNECT_BACKOFF_MIN)
                continue
            if body is None:
                continue
            task_json = body.get("task_json")
            if isinstance(task_json, str):
                yield task_json

    async def send_result(self, task_id: str, result_json: str) -> None:
        await self._request(
            "send_result", {"task_id": task_id, "result_json": result_json},
        )

    async def get_result(self, task_id: str, timeout: float | None = None) -> str:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            wait_seconds = (
                _DEFAULT_LONG_POLL_SECONDS if remaining is None
                else min(_DEFAULT_LONG_POLL_SECONDS, remaining)
            )
            body = await self._request(
                "get_result",
                {"task_id": task_id, "wait_seconds": wait_seconds},
                timeout=(wait_seconds + 10.0) if wait_seconds else 10.0,
            )
            if body is not None:
                result_json = body.get("result_json")
                if isinstance(result_json, str):
                    return result_json
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for result of task {task_id}")

    async def try_get_result(self, task_id: str) -> str | None:
        body = await self._request(
            "get_result", {"task_id": task_id, "wait_seconds": 0.0},
        )
        if body is None:
            return None
        result_json = body.get("result_json")
        return result_json if isinstance(result_json, str) else None

    async def send_heartbeat(self, task_id: str) -> None:
        await self._request("send_heartbeat", {"task_id": task_id})

    async def heartbeat_and_check_cancel(self, task_id: str) -> bool:
        body = await self._request("send_heartbeat", {"task_id": task_id})
        return bool(body and body.get("cancelled"))

    async def get_heartbeat(self, task_id: str) -> float | None:
        body = await self._request("get_heartbeat", {"task_id": task_id})
        if body is None:
            return None
        raw = body.get("heartbeat")
        return float(raw) if isinstance(raw, (int, float)) else None

    async def cancel_task(self, task_id: str) -> None:
        await self._request("cancel_task", {"task_id": task_id})

    async def is_cancelled(self, task_id: str) -> bool:
        body = await self._request("is_cancelled", {"task_id": task_id})
        return bool(body and body.get("cancelled"))

    async def send_log_line(self, task_id: str, line: str) -> None:
        await self._request("send_log_line", {"task_id": task_id, "line": line})

    async def send_progress(self, task_id: str, progress_json: str) -> None:
        await self._request(
            "send_progress",
            {"task_id": task_id, "progress_json": progress_json},
        )

    async def get_progress(self, task_id: str) -> str | None:
        body = await self._request("get_progress", {"task_id": task_id})
        if body is None:
            return None
        progress_json = body.get("progress_json")
        return progress_json if isinstance(progress_json, str) else None

    async def cancel_schedule(self, schedule_id: str) -> None:
        await self._request("cancel_schedule", {"schedule_id": schedule_id})

    async def is_schedule_cancelled(self, schedule_id: str) -> bool:
        body = await self._request(
            "is_schedule_cancelled", {"schedule_id": schedule_id},
        )
        return bool(body and body.get("cancelled"))

    async def check_throttle(self, function_name: str) -> bool:
        body = await self._request(
            "check_throttle", {"function_name": function_name},
        )
        if body is None:
            return True
        return bool(body.get("allowed", True))

    async def record_throttle(self, function_name: str, throttle_seconds: float) -> None:
        await self._request(
            "record_throttle",
            {"function_name": function_name, "throttle_seconds": throttle_seconds},
        )

    async def close(self) -> None:
        self._closed = True
        ws = self._ws
        self._ws = None
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_exception(ConnectionError("backend closed"))
        self._pending.clear()
