"""Tests for the WebSocket backend (offwork client side).

A minimal in-process WS server speaks the broker protocol so we can
exercise every Backend ABC method without standing up cloud_poc.
"""

import asyncio
import json
import socket
from typing import Any
from collections.abc import AsyncIterator

import pytest

pytest.importorskip("websockets")

import websockets  # noqa: E402

from offwork.worker.backends.ws import WebSocketBackend  # noqa: E402
import offwork.worker.remote as _remote  # noqa: E402


class _BrokerState:
    def __init__(self) -> None:
        self.tasks: asyncio.Queue[str] = asyncio.Queue()
        self.results: dict[str, str] = {}
        self.heartbeats: dict[str, float] = {}
        self.cancelled: set[str] = set()
        self.progress: dict[str, str] = {}
        self.schedules: set[str] = set()
        self.throttles: dict[str, float] = {}
        self.api_keys: list[str | None] = []


async def _handle(ws: Any, state: _BrokerState) -> None:
    import time
    hello = json.loads(await ws.recv())
    state.api_keys.append(hello.get("api_key"))
    await ws.send(json.dumps({
        "type": "hello_ok", "protocol": 1, "connection_id": "test",
    }))
    pending: set[asyncio.Task[None]] = set()

    async def _respond(req_id: str, payload: dict[str, Any] | None) -> None:
        await ws.send(json.dumps({
            "type": "response", "id": req_id, "ok": True, "payload": payload,
        }))

    async def _dispatch(frame: dict[str, Any]) -> None:
        op = frame["op"]
        payload = frame.get("payload") or {}
        req_id = frame["id"]
        if op == "submit_task":
            await state.tasks.put(payload["task_json"])
            await _respond(req_id, {"task_id": "t-x"})
        elif op == "claim_task":
            wait = float(payload.get("wait_seconds", 0))
            try:
                task_json = await asyncio.wait_for(state.tasks.get(), timeout=wait)
                await _respond(req_id, {"task_json": task_json})
            except asyncio.TimeoutError:
                await _respond(req_id, None)
        elif op == "send_result":
            state.results[payload["task_id"]] = payload["result_json"]
            await _respond(req_id, {"ok": True})
        elif op == "get_result":
            tid = payload["task_id"]
            wait = float(payload.get("wait_seconds", 0))
            deadline = time.time() + wait
            while tid not in state.results and time.time() < deadline:
                await asyncio.sleep(0.01)
            if tid in state.results:
                await _respond(req_id, {"result_json": state.results[tid]})
            else:
                await _respond(req_id, None)
        elif op == "send_heartbeat":
            tid = payload["task_id"]
            state.heartbeats[tid] = time.time()
            await _respond(req_id, {"ok": True, "cancelled": tid in state.cancelled})
        elif op == "get_heartbeat":
            tid = payload["task_id"]
            hb = state.heartbeats.get(tid)
            await _respond(req_id, {"heartbeat": hb} if hb else None)
        elif op == "cancel_task":
            state.cancelled.add(payload["task_id"])
            await _respond(req_id, {"cancelled": True})
        elif op == "is_cancelled":
            await _respond(req_id, {"cancelled": payload["task_id"] in state.cancelled})
        elif op == "send_progress":
            state.progress[payload["task_id"]] = payload["progress_json"]
            await _respond(req_id, {"ok": True})
        elif op == "get_progress":
            tid = payload["task_id"]
            await _respond(
                req_id, {"progress_json": state.progress[tid]} if tid in state.progress else None,
            )
        elif op == "cancel_schedule":
            state.schedules.add(payload["schedule_id"])
            await _respond(req_id, {"cancelled": True})
        elif op == "is_schedule_cancelled":
            await _respond(req_id, {"cancelled": payload["schedule_id"] in state.schedules})
        elif op == "check_throttle":
            fn = payload["function_name"]
            allowed = time.time() >= state.throttles.get(fn, 0.0)
            await _respond(req_id, {"allowed": allowed})
        elif op == "record_throttle":
            state.throttles[payload["function_name"]] = (
                time.time() + float(payload["throttle_seconds"])
            )
            await _respond(req_id, {"ok": True})
        else:
            await ws.send(json.dumps({
                "type": "response", "id": req_id, "ok": False,
                "error": {"code": "unknown_op", "message": op},
            }))

    try:
        async for raw in ws:
            frame = json.loads(raw)
            if frame.get("type") != "request":
                continue
            task = asyncio.create_task(_dispatch(frame))
            pending.add(task)
            task.add_done_callback(pending.discard)
    except websockets.ConnectionClosed:
        pass


@pytest.fixture(autouse=True)
def _clean_backend() -> AsyncIterator[None]:
    yield
    _remote._active_backend = None
    _remote._atexit_registered = False


@pytest.fixture
async def ws_backend() -> AsyncIterator[tuple[WebSocketBackend, _BrokerState]]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        host, port = sock.getsockname()

    state = _BrokerState()

    async def _serve(ws: Any) -> None:
        await _handle(ws, state)

    server = await websockets.serve(_serve, host, port)
    try:
        backend = WebSocketBackend(f"ws://{host}:{port}?api_key=test-key")
        try:
            yield backend, state
        finally:
            await backend.close()
    finally:
        server.close()
        await server.wait_closed()


class TestWebSocketBackend:
    @pytest.mark.asyncio
    async def test_submit_and_listen(
        self, ws_backend: tuple[WebSocketBackend, _BrokerState],
    ) -> None:
        backend, state = ws_backend
        await backend.submit('{"task": 1}')
        task_iter = backend.listen()
        task_json = await anext(task_iter)
        assert task_json == '{"task": 1}'
        assert state.api_keys[-1] == "test-key"

    @pytest.mark.asyncio
    async def test_send_and_get_result(
        self, ws_backend: tuple[WebSocketBackend, _BrokerState],
    ) -> None:
        backend, _state = ws_backend
        await backend.send_result("t1", '{"ok": true}')
        assert await backend.try_get_result("t1") == '{"ok": true}'
        assert await backend.get_result("t1", timeout=0.5) == '{"ok": true}'

    @pytest.mark.asyncio
    async def test_heartbeat_cancel_progress_and_throttle(
        self, ws_backend: tuple[WebSocketBackend, _BrokerState],
    ) -> None:
        backend, _state = ws_backend
        assert await backend.get_heartbeat("t1") is None
        await backend.send_heartbeat("t1")
        assert await backend.get_heartbeat("t1") is not None

        await backend.cancel_task("t1")
        assert await backend.is_cancelled("t1") is True
        assert await backend.heartbeat_and_check_cancel("t1") is True

        await backend.send_progress("t1", '{"current": 1}')
        assert await backend.get_progress("t1") == '{"current": 1}'

        assert await backend.check_throttle("demo") is True
        await backend.record_throttle("demo", 60)
        assert await backend.check_throttle("demo") is False

    @pytest.mark.asyncio
    async def test_schedule_cancellation_and_connect_dispatch(
        self, ws_backend: tuple[WebSocketBackend, _BrokerState],
    ) -> None:
        backend, _state = ws_backend
        await backend.cancel_schedule("s1")
        assert await backend.is_schedule_cancelled("s1") is True

        ctx = _remote.connect("wss://example.com")
        try:
            assert isinstance(ctx.backend, WebSocketBackend)
        finally:
            await _remote.disconnect()


class TestApiKeyResolution:
    def test_key_from_url(self) -> None:
        backend = WebSocketBackend("ws://host/api/v1/broker/ws?api_key=urlkey")
        assert backend._api_key == "urlkey"
        assert "api_key" not in backend._url

    def test_key_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OFFWORK_API_KEY", "envkey")
        backend = WebSocketBackend("ws://host/api/v1/broker/ws")
        assert backend._api_key == "envkey"

    def test_key_kwarg_beats_url_and_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OFFWORK_API_KEY", "envkey")
        backend = WebSocketBackend(
            "ws://host/api/v1/broker/ws?api_key=urlkey", api_key="kwargkey",
        )
        assert backend._api_key == "kwargkey"

    def test_url_beats_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OFFWORK_API_KEY", "envkey")
        backend = WebSocketBackend("ws://host/api/v1/broker/ws?api_key=urlkey")
        assert backend._api_key == "urlkey"

    def test_no_key_anywhere(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OFFWORK_API_KEY", raising=False)
        backend = WebSocketBackend("ws://host/api/v1/broker/ws")
        assert backend._api_key is None
