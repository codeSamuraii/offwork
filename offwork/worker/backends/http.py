"""HTTP(S) backend for hosted broker deployments."""

import json
import time
import asyncio
import socket
import threading
from collections import deque
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from collections.abc import AsyncIterator

from offwork.worker.backends.base import Backend

_DEFAULT_BROKER_PATH = "/api/v1/broker"
_DEFAULT_LONG_POLL_SECONDS = 30.0
# Cap on concurrent persistent TCP connections per backend instance.
# 8 is plenty for a worker that runs claim (long-poll) + heartbeat +
# progress + a result write in parallel without ever serialising.
_MAX_POOL_SIZE = 8


class HttpBackend(Backend):
    """HTTP(S)-based backend for hosted offwork brokers.

    Uses a small pool of persistent ``http.client`` connections so each
    request reuses an already-open TCP socket. ``urllib.request.urlopen``
    (the previous implementation) opens a fresh socket per call, which
    means every API hit pays a TCP handshake — and on lossy paths a
    dropped SYN turns into a 1/3/7 s retransmit stall. With keepalive
    enabled the socket is established once and amortised over many
    requests.

    The base URL can point either at the broker root or at the service root;
    when no path is provided, ``/api/v1/broker`` is assumed.

    Authentication is currently supported via an API key, which can be provided by
    including ``?api_key=...`` in the URL and the backend will move it into the
    ``X-Offwork-API-Key`` request header.
    """

    def __init__(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(f"Unsupported HTTP backend scheme: {parsed.scheme!r}")

        query_items = parse_qsl(parsed.query, keep_blank_values=True)
        api_key = ""
        filtered_query: list[tuple[str, str]] = []
        for key, value in query_items:
            if key == "api_key" and not api_key:
                api_key = value
                continue
            filtered_query.append((key, value))

        path = parsed.path.rstrip("/") or _DEFAULT_BROKER_PATH
        # _base_url is kept only for ``_url()`` (still used to build the
        # request path with the broker root prefix). Host/port/scheme
        # are also cached separately for the connection pool.
        self._base_url = urlunparse(parsed._replace(path=path, query=urlencode(filtered_query)))
        self._api_key = api_key or None
        self._scheme = parsed.scheme
        self._host = parsed.hostname or ""
        self._port = parsed.port or (443 if self._scheme == "https" else 80)
        self._path_prefix = path
        # Encoded base query string (everything except api_key); we
        # always append per-request query params onto this.
        self._base_query = urlencode(filtered_query)

        # Pool of idle, keepalive-ready connections. Guarded by a plain
        # threading.Lock because acquire/release runs on the
        # ``asyncio.to_thread`` worker pool, not the event loop.
        self._pool: deque[HTTPConnection] = deque()
        self._pool_lock = threading.Lock()
        self._pool_size = 0  # total connections (idle + in-use)

    # ------------------------------------------------------------------ #
    # Connection pool (called from the threadpool, not the event loop)
    # ------------------------------------------------------------------ #

    def _new_connection(self, timeout: float | None) -> HTTPConnection:
        cls = HTTPSConnection if self._scheme == "https" else HTTPConnection
        # http.client treats timeout=None as "no timeout"; we pass it
        # through unchanged so long-poll calls can supply their own.
        return cls(self._host, self._port, timeout=timeout)

    def _acquire(self, timeout: float | None) -> HTTPConnection:
        with self._pool_lock:
            if self._pool:
                conn = self._pool.popleft()
                # Reset the socket timeout for the current request — the
                # value carried over from the previous user is stale.
                if conn.sock is not None:
                    conn.sock.settimeout(timeout)
                return conn
            self._pool_size += 1
        return self._new_connection(timeout)

    def _release(self, conn: HTTPConnection, *, broken: bool) -> None:
        if broken:
            try:
                conn.close()
            except Exception:
                pass
            with self._pool_lock:
                self._pool_size = max(0, self._pool_size - 1)
            return
        with self._pool_lock:
            if len(self._pool) >= _MAX_POOL_SIZE:
                # Pool full — drop this one rather than grow unbounded.
                self._pool_size = max(0, self._pool_size - 1)
                try:
                    conn.close()
                except Exception:
                    pass
                return
            self._pool.append(conn)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Connection": "keep-alive",
        }
        if self._api_key:
            headers["X-Offwork-API-Key"] = self._api_key
        return headers

    def _url(self, suffix: str, query: dict[str, str | float | int] | None = None) -> str:
        url = f"{self._base_url}{suffix}"
        if not query:
            return url
        encoded = urlencode({key: str(value) for key, value in query.items()})
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}{encoded}"

    def _request_path(
        self, suffix: str, query: dict[str, str | float | int] | None,
    ) -> str:
        path = f"{self._path_prefix}{suffix}"
        params = dict(parse_qsl(self._base_query, keep_blank_values=True))
        if query:
            params.update({k: str(v) for k, v in query.items()})
        if not params:
            return path
        return f"{path}?{urlencode(params)}"

    def _do_request(
        self,
        method: str,
        suffix: str,
        *,
        payload: dict[str, Any] | None = None,
        query: dict[str, str | float | int] | None = None,
        timeout: float | None = None,
        allow_not_found: bool = False,
    ) -> tuple[int, Any | None]:
        data = b"" if payload is None else json.dumps(payload).encode("utf-8")
        path = self._request_path(suffix, query)
        headers = self._headers()
        if data:
            headers["Content-Length"] = str(len(data))

        # Try once on a pooled connection. If the server closed the
        # socket between calls (common with long-idle keepalive), retry
        # once on a fresh connection.
        for attempt in (0, 1):
            conn = self._acquire(timeout)
            broken = False
            try:
                conn.request(method, path, body=data or None, headers=headers)
                response = conn.getresponse()
                status = response.status
                raw = response.read()
            except (HTTPException, ConnectionError, OSError, socket.timeout) as exc:
                broken = True
                self._release(conn, broken=True)
                if attempt == 0 and isinstance(exc, (HTTPException, ConnectionResetError, BrokenPipeError)):
                    # Stale keepalive socket — retry once with a new one.
                    continue
                if isinstance(exc, socket.timeout):
                    raise ConnectionError(f"HTTP backend connection failed: timed out") from exc
                raise ConnectionError(f"HTTP backend connection failed: {exc}") from exc
            finally:
                if not broken:
                    self._release(conn, broken=False)

            if status in {204, 404} and allow_not_found:
                return status, None
            if status >= 400:
                message = raw.decode("utf-8", errors="replace") if raw else ""
                raise RuntimeError(
                    f"HTTP backend request failed: {method} {suffix} -> {status} {message}"
                )
            if not raw:
                return status, None
            return status, json.loads(raw.decode("utf-8"))

        # Unreachable: the for-loop either returns or raises.
        raise ConnectionError("HTTP backend connection failed: retry exhausted")

    async def _request(
        self,
        method: str,
        suffix: str,
        *,
        payload: dict[str, Any] | None = None,
        query: dict[str, str | float | int] | None = None,
        timeout: float | None = None,
        allow_not_found: bool = False,
    ) -> tuple[int, Any | None]:
        return await asyncio.to_thread(
            self._do_request,
            method,
            suffix,
            payload=payload,
            query=query,
            timeout=timeout,
            allow_not_found=allow_not_found,
        )

    async def submit(self, task_json: str) -> None:
        await self._request("POST", "/tasks", payload={"task_json": task_json})

    async def listen(self) -> AsyncIterator[str]:
        while True:
            _status, body = await self._request(
                "POST",
                "/tasks/claim",
                payload={"wait_seconds": _DEFAULT_LONG_POLL_SECONDS},
                timeout=_DEFAULT_LONG_POLL_SECONDS + 5.0,
                allow_not_found=True,
            )
            if body is None:
                continue
            task_json = body.get("task_json")
            if isinstance(task_json, str):
                yield task_json

    async def send_result(self, task_id: str, result_json: str) -> None:
        await self._request(
            "POST",
            f"/tasks/{task_id}/result",
            payload={"result_json": result_json},
        )

    async def get_result(self, task_id: str, timeout: float | None = None) -> str:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            wait_seconds = _DEFAULT_LONG_POLL_SECONDS if remaining is None else min(_DEFAULT_LONG_POLL_SECONDS, remaining)
            status, body = await self._request(
                "GET",
                f"/tasks/{task_id}/result",
                query={"wait_seconds": wait_seconds},
                timeout=(wait_seconds + 5.0) if wait_seconds else 5.0,
                allow_not_found=True,
            )
            if body is not None:
                result_json = body.get("result_json")
                if isinstance(result_json, str):
                    return result_json
            if deadline is not None and (status == 204 or time.monotonic() >= deadline):
                raise TimeoutError(f"Timed out waiting for result of task {task_id}")

    async def try_get_result(self, task_id: str) -> str | None:
        _status, body = await self._request(
            "GET",
            f"/tasks/{task_id}/result",
            query={"wait_seconds": 0},
            allow_not_found=True,
        )
        if body is None:
            return None
        result_json = body.get("result_json")
        return result_json if isinstance(result_json, str) else None

    async def send_heartbeat(self, task_id: str) -> None:
        await self._request("POST", f"/tasks/{task_id}/heartbeat")

    async def heartbeat_and_check_cancel(self, task_id: str) -> bool:
        _status, body = await self._request(
            "POST", f"/tasks/{task_id}/heartbeat", allow_not_found=True,
        )
        return bool(body and body.get("cancelled"))

    async def get_heartbeat(self, task_id: str) -> float | None:
        _status, body = await self._request(
            "GET", f"/tasks/{task_id}/heartbeat", allow_not_found=True,
        )
        if body is None:
            return None
        raw = body.get("heartbeat")
        return float(raw) if isinstance(raw, (int, float)) else None

    async def cancel_task(self, task_id: str) -> None:
        await self._request("POST", f"/tasks/{task_id}/cancel")

    async def is_cancelled(self, task_id: str) -> bool:
        _status, body = await self._request(
            "GET", f"/tasks/{task_id}/cancel", allow_not_found=True,
        )
        return bool(body and body.get("cancelled"))

    async def send_progress(self, task_id: str, progress_json: str) -> None:
        await self._request(
            "POST",
            f"/tasks/{task_id}/progress",
            payload={"progress_json": progress_json},
        )

    async def get_progress(self, task_id: str) -> str | None:
        _status, body = await self._request(
            "GET", f"/tasks/{task_id}/progress", allow_not_found=True,
        )
        if body is None:
            return None
        progress_json = body.get("progress_json")
        return progress_json if isinstance(progress_json, str) else None

    async def cancel_schedule(self, schedule_id: str) -> None:
        await self._request("POST", f"/schedules/{schedule_id}/cancel")

    async def is_schedule_cancelled(self, schedule_id: str) -> bool:
        _status, body = await self._request(
            "GET", f"/schedules/{schedule_id}/cancel", allow_not_found=True,
        )
        return bool(body and body.get("cancelled"))

    async def check_throttle(self, function_name: str) -> bool:
        _status, body = await self._request(
            "GET",
            "/throttle/check",
            query={"function_name": function_name},
            allow_not_found=True,
        )
        return True if body is None else bool(body.get("allowed", True))

    async def record_throttle(self, function_name: str, throttle_seconds: float) -> None:
        await self._request(
            "POST",
            "/throttle/record",
            payload={
                "function_name": function_name,
                "throttle_seconds": throttle_seconds,
            },
        )

    async def close(self) -> None:
        with self._pool_lock:
            pool = list(self._pool)
            self._pool.clear()
            self._pool_size = 0
        for conn in pool:
            try:
                conn.close()
            except Exception:
                pass
