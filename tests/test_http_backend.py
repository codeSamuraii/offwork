"""Tests for the HTTP backend."""

import json
import time
import queue
import socket
import threading
from typing import Any
from http import HTTPStatus
from urllib.parse import parse_qs, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections.abc import AsyncIterator

import pytest

from seeya.worker.backends.http import HttpBackend
import seeya.worker.remote as _remote


class _BrokerState:
    def __init__(self) -> None:
        self.tasks: queue.Queue[str] = queue.Queue()
        self.results: dict[str, str] = {}
        self.heartbeats: dict[str, float] = {}
        self.cancelled: set[str] = set()
        self.progress: dict[str, str] = {}
        self.schedules: set[str] = set()
        self.throttles: dict[str, float] = {}
        self.headers: list[str | None] = []
        self.lock = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    state: _BrokerState

    def log_message(self, format: str, *args: object) -> None:
        return

    def _json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _send_json(self, status: int, payload: dict[str, Any] | None = None) -> None:
        self.send_response(status)
        if payload is None:
            self.end_headers()
            return
        data = json.dumps(payload).encode("utf-8")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _path(self) -> str:
        return urlparse(self.path).path

    def _query(self) -> dict[str, list[str]]:
        return parse_qs(urlparse(self.path).query)

    def _record_header(self) -> None:
        with self.state.lock:
            self.state.headers.append(self.headers.get("X-Seeya-API-Key"))

    def do_POST(self) -> None:  # noqa: N802
        self._record_header()
        path = self._path()
        body = self._json_body()
        if path == "/api/v1/broker/tasks":
            self.state.tasks.put(body["task_json"])
            self._send_json(HTTPStatus.ACCEPTED, {"ok": True})
            return
        if path == "/api/v1/broker/tasks/claim":
            wait_seconds = float(body.get("wait_seconds", 0))
            try:
                task_json = self.state.tasks.get(timeout=wait_seconds)
            except queue.Empty:
                self._send_json(HTTPStatus.NO_CONTENT)
                return
            self._send_json(HTTPStatus.OK, {"task_json": task_json})
            return
        if path.endswith("/result") and "/tasks/" in path:
            task_id = path.split("/tasks/", 1)[1].split("/", 1)[0]
            self.state.results[task_id] = body["result_json"]
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        if path.endswith("/heartbeat") and "/tasks/" in path:
            task_id = path.split("/tasks/", 1)[1].split("/", 1)[0]
            self.state.heartbeats[task_id] = time.time()
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        if path.endswith("/cancel") and "/tasks/" in path:
            task_id = path.split("/tasks/", 1)[1].split("/", 1)[0]
            self.state.cancelled.add(task_id)
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        if path.endswith("/progress") and "/tasks/" in path:
            task_id = path.split("/tasks/", 1)[1].split("/", 1)[0]
            self.state.progress[task_id] = body["progress_json"]
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        if path.endswith("/cancel") and "/schedules/" in path:
            schedule_id = path.split("/schedules/", 1)[1].split("/", 1)[0]
            self.state.schedules.add(schedule_id)
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        if path == "/api/v1/broker/throttle/record":
            fn = body["function_name"]
            self.state.throttles[fn] = time.time() + float(body["throttle_seconds"])
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"detail": path})

    def do_GET(self) -> None:  # noqa: N802
        self._record_header()
        path = self._path()
        query = self._query()
        if path.endswith("/result") and "/tasks/" in path:
            task_id = path.split("/tasks/", 1)[1].split("/", 1)[0]
            wait_seconds = float(query.get("wait_seconds", ["0"])[0])
            deadline = time.time() + wait_seconds
            while task_id not in self.state.results and time.time() < deadline:
                time.sleep(0.01)
            result_json = self.state.results.get(task_id)
            if result_json is None:
                self._send_json(HTTPStatus.NO_CONTENT)
                return
            self._send_json(HTTPStatus.OK, {"result_json": result_json})
            return
        if path.endswith("/heartbeat") and "/tasks/" in path:
            task_id = path.split("/tasks/", 1)[1].split("/", 1)[0]
            heartbeat = self.state.heartbeats.get(task_id)
            if heartbeat is None:
                self._send_json(HTTPStatus.NO_CONTENT)
                return
            self._send_json(HTTPStatus.OK, {"heartbeat": heartbeat})
            return
        if path.endswith("/cancel") and "/tasks/" in path:
            task_id = path.split("/tasks/", 1)[1].split("/", 1)[0]
            self._send_json(HTTPStatus.OK, {"cancelled": task_id in self.state.cancelled})
            return
        if path.endswith("/progress") and "/tasks/" in path:
            task_id = path.split("/tasks/", 1)[1].split("/", 1)[0]
            progress_json = self.state.progress.get(task_id)
            if progress_json is None:
                self._send_json(HTTPStatus.NO_CONTENT)
                return
            self._send_json(HTTPStatus.OK, {"progress_json": progress_json})
            return
        if path.endswith("/cancel") and "/schedules/" in path:
            schedule_id = path.split("/schedules/", 1)[1].split("/", 1)[0]
            self._send_json(HTTPStatus.OK, {"cancelled": schedule_id in self.state.schedules})
            return
        if path == "/api/v1/broker/throttle/check":
            fn = query["function_name"][0]
            allowed = time.time() >= self.state.throttles.get(fn, 0.0)
            self._send_json(HTTPStatus.OK, {"allowed": allowed})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"detail": path})


@pytest.fixture(autouse=True)
def _clean_backend() -> AsyncIterator[None]:
    yield
    _remote._active_backend = None
    _remote._atexit_registered = False


@pytest.fixture
def http_backend() -> AsyncIterator[tuple[HttpBackend, _BrokerState]]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        host, port = sock.getsockname()

    state = _BrokerState()
    _Handler.state = state
    server = ThreadingHTTPServer((host, port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        backend = HttpBackend(f"http://{host}:{port}?api_key=test-key")
        yield backend, state
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


class TestHttpBackend:
    @pytest.mark.asyncio
    async def test_submit_and_listen(self, http_backend: tuple[HttpBackend, _BrokerState]) -> None:
        backend, state = http_backend
        await backend.submit('{"task": 1}')
        task_iter = backend.listen()
        task_json = await anext(task_iter)
        assert task_json == '{"task": 1}'
        assert state.headers[-1] == "test-key"

    @pytest.mark.asyncio
    async def test_send_and_get_result(self, http_backend: tuple[HttpBackend, _BrokerState]) -> None:
        backend, _state = http_backend
        await backend.send_result("t1", '{"ok": true}')
        assert await backend.try_get_result("t1") == '{"ok": true}'
        assert await backend.get_result("t1", timeout=0.1) == '{"ok": true}'

    @pytest.mark.asyncio
    async def test_heartbeat_cancel_progress_and_throttle(self, http_backend: tuple[HttpBackend, _BrokerState]) -> None:
        backend, _state = http_backend
        assert await backend.get_heartbeat("t1") is None
        await backend.send_heartbeat("t1")
        assert await backend.get_heartbeat("t1") is not None

        await backend.cancel_task("t1")
        assert await backend.is_cancelled("t1") is True

        await backend.send_progress("t1", '{"current": 1}')
        assert await backend.get_progress("t1") == '{"current": 1}'

        assert await backend.check_throttle("demo") is True
        await backend.record_throttle("demo", 60)
        assert await backend.check_throttle("demo") is False

    @pytest.mark.asyncio
    async def test_schedule_cancellation_and_connect_dispatch(self, http_backend: tuple[HttpBackend, _BrokerState]) -> None:
        backend, _state = http_backend
        await backend.cancel_schedule("s1")
        assert await backend.is_schedule_cancelled("s1") is True

        connected = _remote.connect("https://example.com")
        try:
            assert isinstance(connected, HttpBackend)
        finally:
            await _remote.disconnect()
