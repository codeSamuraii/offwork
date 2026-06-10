"""In-process WebSocket broker for tests.

Speaks the offwork broker WS protocol with in-memory state, no Mongo,
no RabbitMQ. Usable as a session fixture or as a standalone script::

    python -m tests.fixtures._ws_broker --host 127.0.0.1 --port 9876
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from collections import defaultdict
from typing import Any

import websockets


logger = logging.getLogger("ws-broker")


class _BrokerState:
    """In-memory broker state shared across all connections."""

    def __init__(self) -> None:
        # task queue ordered by scheduled_at (default 0 = immediate)
        self.queued: list[tuple[float, str, str]] = []  # (scheduled_at, task_id, task_json)
        self.results: dict[str, str] = {}
        self.heartbeats: dict[str, float] = {}
        self.cancelled: set[str] = set()
        self.progress: dict[str, str] = {}
        self.yields: dict[str, list[str]] = {}
        self.schedules_cancelled: set[str] = set()
        self.throttles: dict[str, float] = {}
        self.task_waiters: list[asyncio.Event] = []
        self.result_waiters: dict[str, list[asyncio.Event]] = defaultdict(list)
        self.lock = asyncio.Lock()

    def _wake_tasks(self) -> None:
        for ev in self.task_waiters:
            ev.set()

    def _wake_result(self, task_id: str) -> None:
        for ev in self.result_waiters.get(task_id, ()):
            ev.set()

    async def submit(self, task_json: str) -> str:
        data = json.loads(task_json)
        # Signed envelope: unwrap to access the inner task fields.
        if "task" in data and "client_id" in data:
            inner = data["task"]
        else:
            inner = data
        task_id = inner.get("id") or inner.get("task_id") or f"t-{time.time_ns()}"
        scheduled_at = float(inner.get("scheduled_at") or 0.0)
        async with self.lock:
            self.queued.append((scheduled_at, task_id, task_json))
            self.queued.sort(key=lambda x: x[0])
        self._wake_tasks()
        return task_id

    async def claim(self, wait_seconds: float) -> str | None:
        deadline = time.monotonic() + wait_seconds
        event = asyncio.Event()
        self.task_waiters.append(event)
        try:
            while True:
                async with self.lock:
                    now = time.time()
                    for i, (sched, _tid, _tj) in enumerate(self.queued):
                        if sched <= now:
                            _sched, _tid, task_json = self.queued.pop(i)
                            return task_json
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                # Wake periodically so scheduled_at-gated tasks aren't
                # stuck waiting on a signal that may already have fired.
                try:
                    await asyncio.wait_for(event.wait(), timeout=min(remaining, 0.25))
                except asyncio.TimeoutError:
                    pass
                event.clear()
        finally:
            try:
                self.task_waiters.remove(event)
            except ValueError:
                pass

    async def store_result(self, task_id: str, result_json: str) -> None:
        self.results[task_id] = result_json
        self._wake_result(task_id)

    async def get_result(self, task_id: str, wait_seconds: float) -> str | None:
        deadline = time.monotonic() + wait_seconds
        event = asyncio.Event()
        self.result_waiters[task_id].append(event)
        try:
            while True:
                if task_id in self.results:
                    return self.results[task_id]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(event.wait(), timeout=min(remaining, 5.0))
                except asyncio.TimeoutError:
                    pass
                event.clear()
        finally:
            bucket = self.result_waiters.get(task_id, [])
            try:
                bucket.remove(event)
            except ValueError:
                pass
            if not bucket:
                self.result_waiters.pop(task_id, None)


async def _dispatch(
    state: _BrokerState, op: str, payload: dict[str, Any],
) -> dict[str, Any] | None:
    if op == "submit_task":
        task_id = await state.submit(payload["task_json"])
        return {"task_id": task_id}
    if op == "claim_task":
        wait = float(payload.get("wait_seconds", 0.0))
        task_json = await state.claim(wait)
        return {"task_json": task_json} if task_json is not None else None
    if op == "send_result":
        await state.store_result(payload["task_id"], payload["result_json"])
        return {"ok": True}
    if op == "get_result":
        wait = float(payload.get("wait_seconds", 0.0))
        result_json = await state.get_result(payload["task_id"], wait)
        return {"result_json": result_json} if result_json is not None else None
    if op == "send_heartbeat":
        tid = payload["task_id"]
        state.heartbeats[tid] = time.time()
        return {"ok": True, "cancelled": tid in state.cancelled}
    if op == "get_heartbeat":
        tid = payload["task_id"]
        hb = state.heartbeats.get(tid)
        return {"heartbeat": hb} if hb is not None else None
    if op == "cancel_task":
        state.cancelled.add(payload["task_id"])
        return {"cancelled": True}
    if op == "is_cancelled":
        return {"cancelled": payload["task_id"] in state.cancelled}
    if op == "send_log_line":
        return None
    if op == "send_progress":
        state.progress[payload["task_id"]] = payload["progress_json"]
        return {"ok": True}
    if op == "get_progress":
        tid = payload["task_id"]
        if tid in state.progress:
            return {"progress_json": state.progress[tid]}
        return None
    if op == "send_yield":
        buf = state.yields.setdefault(payload["task_id"], [])
        seq = int(payload["seq"])
        while len(buf) <= seq:
            buf.append("")
        buf[seq] = payload["value_json"]
        return {"ok": True}
    if op == "get_yields":
        buf = state.yields.get(payload["task_id"], [])
        after = int(payload.get("after_seq", -1))
        items = [
            [i, buf[i]]
            for i in range(after + 1, len(buf))
            if buf[i] != ""
        ]
        return {"yields": items}
    if op == "cancel_schedule":
        state.schedules_cancelled.add(payload["schedule_id"])
        return {"cancelled": True}
    if op == "is_schedule_cancelled":
        return {"cancelled": payload["schedule_id"] in state.schedules_cancelled}
    if op == "check_throttle":
        fn = payload["function_name"]
        expire = state.throttles.get(fn, 0.0)
        return {"allowed": time.time() >= expire}
    if op == "record_throttle":
        state.throttles[payload["function_name"]] = (
            time.time() + float(payload["throttle_seconds"])
        )
        return {"ok": True}
    raise ValueError(f"unknown op: {op}")


async def _handle_connection(ws: Any, state: _BrokerState) -> None:
    try:
        hello = json.loads(await ws.recv())
    except Exception:
        await ws.close(code=4400)
        return
    if hello.get("type") != "hello" or hello.get("protocol") != 1:
        await ws.close(code=4400)
        return
    await ws.send(json.dumps({
        "type": "hello_ok", "protocol": 1, "connection_id": "test",
    }))

    send_lock = asyncio.Lock()
    pending: set[asyncio.Task[None]] = set()

    async def _send(frame: dict[str, Any]) -> None:
        async with send_lock:
            await ws.send(json.dumps(frame))

    async def _handle_request(req_id: str, op: str, payload: dict[str, Any]) -> None:
        try:
            result = await _dispatch(state, op, payload)
            await _send({
                "type": "response", "id": req_id, "ok": True, "payload": result,
            })
        except Exception as exc:
            await _send({
                "type": "response", "id": req_id, "ok": False,
                "error": {"code": "error", "message": str(exc)},
            })

    try:
        async for raw in ws:
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if frame.get("type") != "request":
                continue
            req_id = str(frame.get("id") or "")
            op = str(frame.get("op") or "")
            payload = frame.get("payload") or {}
            if not req_id or not op:
                continue
            task = asyncio.create_task(_handle_request(req_id, op, payload))
            pending.add(task)
            task.add_done_callback(pending.discard)
    except websockets.ConnectionClosed:
        pass
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


async def serve(host: str, port: int) -> None:
    state = _BrokerState()

    async def _h(ws: Any) -> None:
        await _handle_connection(ws, state)

    server = await websockets.serve(_h, host, port, max_size=None)
    logger.info("ws broker listening on ws://%s:%d", host, port)
    try:
        await server.wait_closed()
    finally:
        server.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9876)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        asyncio.run(serve(args.host, args.port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
