"""HTTP(S) backend for hosted broker deployments."""

import json
import time
import base64
import asyncio
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen
from collections.abc import AsyncIterator

from offwork.worker.backends.base import Backend

_DEFAULT_BROKER_PATH = "/api/v1/broker"
_DEFAULT_LONG_POLL_SECONDS = 30.0


class HttpBackend(Backend):
    """HTTP(S)-based backend for hosted offwork brokers.

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
        self._base_url = urlunparse(parsed._replace(path=path, query=urlencode(filtered_query)))
        self._api_key = api_key or None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
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
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            self._url(suffix, query=query),
            data=data,
            method=method,
            headers=self._headers(),
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
                if not raw:
                    return response.status, None
                return response.status, json.loads(raw.decode("utf-8"))
        except HTTPError as exc:
            if exc.code in {204, 404} and allow_not_found:
                return exc.code, None
            message = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"HTTP backend request failed: {method} {suffix} -> {exc.code} {message}"
            ) from exc
        except URLError as exc:
            raise ConnectionError(f"HTTP backend connection failed: {exc.reason}") from exc

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
        return None
