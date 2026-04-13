"""Async-native TCP backend for same-machine IPC.

A lightweight broker server built on :mod:`asyncio` TCP streams handles
task dispatch, result routing, and heartbeats -- no threads, no
:mod:`multiprocessing`, no external services.

URL scheme: ``local://host:port``  (default ``local://127.0.0.1:9748``)
"""
from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import logging
import socket
import struct
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

from pyfuse.worker.backends.base import Backend

logger = logging.getLogger(__name__)

_HEADER = struct.Struct("!I")  # 4-byte big-endian length prefix
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 9748


# ---------------------------------------------------------------------------
# Wire protocol
# ---------------------------------------------------------------------------


async def _send_msg(writer: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
    """Send a length-prefixed JSON message."""
    payload = json.dumps(obj, separators=(",", ":")).encode()
    writer.write(_HEADER.pack(len(payload)) + payload)
    await writer.drain()


async def _recv_msg(reader: asyncio.StreamReader) -> dict[str, Any]:
    """Receive a length-prefixed JSON message.

    Raises :class:`asyncio.IncompleteReadError` on EOF.
    """
    raw = await reader.readexactly(_HEADER.size)
    (length,) = _HEADER.unpack(raw)
    data = await reader.readexactly(length)
    result: dict[str, Any] = json.loads(data)
    return result


# ---------------------------------------------------------------------------
# Broker server (pure asyncio)
# ---------------------------------------------------------------------------


class _Broker:
    """Task broker backed entirely by asyncio primitives."""

    def __init__(self) -> None:
        self._tasks: asyncio.Queue[str] = asyncio.Queue()
        self._results: dict[str, asyncio.Queue[str]] = {}
        self._heartbeats: dict[str, float] = {}
        self._cancelled: set[str] = set()
        self._progress: dict[str, str] = {}
        self._result_subs: list[asyncio.Queue[str]] = []

    def _result_slot(self, task_id: str) -> asyncio.Queue[str]:
        if task_id not in self._results:
            self._results[task_id] = asyncio.Queue(maxsize=1)
        return self._results[task_id]

    # -- connection handler ----------------------------------------------------

    async def handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            msg = await _recv_msg(reader)
            op = msg.get("op", "")
            if op == "listen":
                await self._stream_tasks(writer)
            elif op == "subscribe":
                await self._stream_results(writer)
            else:
                await self._rpc_loop(msg, reader, writer)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

    async def _rpc_loop(
        self,
        first: dict[str, Any],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        await _send_msg(writer, self._dispatch(first))
        while True:
            msg = await _recv_msg(reader)
            await _send_msg(writer, self._dispatch(msg))

    # -- dispatch (sync -- no awaits, safe for single-threaded asyncio) --------

    def _dispatch(self, msg: dict[str, Any]) -> dict[str, Any]:
        op = msg["op"]
        if op == "submit":
            self._tasks.put_nowait(msg["data"])
            return {"ok": True}
        if op == "result_put":
            try:
                self._result_slot(msg["task_id"]).put_nowait(msg["data"])
            except asyncio.QueueFull:
                pass  # first result wins (e.g., cancel before worker result)
            for sub in self._result_subs:
                sub.put_nowait(msg["task_id"])
            return {"ok": True}
        if op == "result_try":
            try:
                return {"ok": True, "data": self._result_slot(msg["task_id"]).get_nowait()}
            except asyncio.QueueEmpty:
                return {"ok": True, "data": None}
        if op == "hb_put":
            self._heartbeats[msg["task_id"]] = msg["ts"]
            return {"ok": True}
        if op == "hb_get":
            return {"ok": True, "data": self._heartbeats.get(msg["task_id"])}
        if op == "hb_batch":
            return {
                "ok": True,
                "data": {tid: self._heartbeats.get(tid) for tid in msg["task_ids"]},
            }
        if op == "cancel":
            self._cancelled.add(msg["task_id"])
            return {"ok": True}
        if op == "is_cancelled":
            return {"ok": True, "data": msg["task_id"] in self._cancelled}
        if op == "progress_put":
            self._progress[msg["task_id"]] = msg["data"]
            return {"ok": True}
        if op == "progress_get":
            return {"ok": True, "data": self._progress.get(msg["task_id"])}
        return {"ok": False, "error": f"unknown op: {op}"}

    # -- streaming handlers ----------------------------------------------------

    async def _stream_tasks(self, writer: asyncio.StreamWriter) -> None:
        while True:
            try:
                task = await asyncio.wait_for(self._tasks.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await _send_msg(writer, {"data": task})
            except (ConnectionError, OSError):
                # Client gone -- put the task back so another listener gets it.
                self._tasks.put_nowait(task)
                return

    async def _stream_results(self, writer: asyncio.StreamWriter) -> None:
        q: asyncio.Queue[str] = asyncio.Queue()
        self._result_subs.append(q)
        try:
            while True:
                try:
                    task_id = await asyncio.wait_for(q.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                try:
                    await _send_msg(writer, {"data": task_id})
                except (ConnectionError, OSError):
                    return
        finally:
            self._result_subs.remove(q)


async def run_broker(host: str, port: int) -> None:
    """Start the broker TCP server (runs forever)."""
    broker = _Broker()
    server = await asyncio.start_server(broker.handle, host, port)
    logger.info("Local broker listening on %s:%d", host, port)
    async with server:
        await server.serve_forever()


def _broker_main(host: str, port: int) -> None:
    """Entry point for the broker subprocess."""
    asyncio.run(run_broker(host, port))


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------


def _parse_local_url(url: str) -> tuple[str, int]:
    parsed = urlparse(url)
    host = parsed.hostname or _DEFAULT_HOST
    port = parsed.port or _DEFAULT_PORT
    return host, port


# ---------------------------------------------------------------------------
# LocalBackend
# ---------------------------------------------------------------------------


class LocalBackend(Backend):
    """Async-native TCP backend for same-machine IPC.

    A lightweight broker process handles task dispatch, result routing,
    and heartbeats over TCP on localhost.  Every I/O operation is a
    native :mod:`asyncio` coroutine -- no threads anywhere.

    Parameters
    ----------
    url
        ``local://host:port``  (default ``local://127.0.0.1:9748``).
    server
        ``True`` to start the broker, ``False`` to connect to an
        existing one, ``None`` (default) to auto-detect.
    """

    def __init__(
        self,
        url: str = "local://localhost",
        *,
        server: bool | None = None,
    ) -> None:
        self._host, self._port = _parse_local_url(url)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._server_proc: subprocess.Popen[bytes] | None = None

        self._ensure_broker(server)
        logger.info(
            "LocalBackend ready (server=%s, %s:%d)",
            self._server_proc is not None, self._host, self._port,
        )

    # -- broker lifecycle ------------------------------------------------------

    def _ensure_broker(self, server: bool | None) -> None:
        if server is False:
            return
        if self._probe():
            return
        self._start_broker()

    def _probe(self) -> bool:
        """Check whether a broker is already accepting connections."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            s.connect((self._host, self._port))
            s.close()
            return True
        except (ConnectionRefusedError, OSError):
            return False

    def _start_broker(self) -> None:
        self._server_proc = subprocess.Popen(
            [
                sys.executable, "-c",
                "from pyfuse.worker.backends.local import _broker_main; "
                f"_broker_main({self._host!r}, {self._port})",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        atexit.register(self._kill_broker)
        for _ in range(50):  # up to 5 s
            if self._probe():
                return
            time.sleep(0.1)
        raise ConnectionError(
            f"Broker failed to start on {self._host}:{self._port}"
        )

    def _kill_broker(self) -> None:
        p = self._server_proc
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        self._server_proc = None

    # -- TCP connection --------------------------------------------------------

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self._reader is None or self._writer is None:
            self._reader, self._writer = await asyncio.open_connection(
                self._host, self._port,
            )
        return self._reader, self._writer

    async def _request(self, msg: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            try:
                reader, writer = await self._connect()
                await _send_msg(writer, msg)
                return await _recv_msg(reader)
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                if self._writer is not None:
                    self._writer.close()
                self._reader = self._writer = None
                raise

    # -- Backend interface -----------------------------------------------------

    async def submit(self, task_json: str) -> None:
        await self._request({"op": "submit", "data": task_json})

    async def listen(self) -> AsyncIterator[str]:
        reader, writer = await asyncio.open_connection(self._host, self._port)
        try:
            await _send_msg(writer, {"op": "listen"})
            while True:
                msg = await _recv_msg(reader)
                yield msg["data"]
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            return
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

    async def send_result(self, task_id: str, result_json: str) -> None:
        await self._request({
            "op": "result_put", "task_id": task_id, "data": result_json,
        })

    async def get_result(self, task_id: str, timeout: float | None = None) -> str:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            raw = await self.try_get_result(task_id)
            if raw is not None:
                return raw
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for result of task {task_id}"
                )
            await asyncio.sleep(0.05)

    async def try_get_result(self, task_id: str) -> str | None:
        resp = await self._request({"op": "result_try", "task_id": task_id})
        return resp.get("data")

    async def send_heartbeat(self, task_id: str) -> None:
        await self._request({
            "op": "hb_put", "task_id": task_id, "ts": time.time(),
        })

    async def get_heartbeat(self, task_id: str) -> float | None:
        resp = await self._request({"op": "hb_get", "task_id": task_id})
        return resp.get("data")

    async def get_heartbeats(self, task_ids: list[str]) -> dict[str, float | None]:
        resp = await self._request({"op": "hb_batch", "task_ids": task_ids})
        result: dict[str, float | None] = resp.get("data", {})
        return result

    async def cancel_task(self, task_id: str) -> None:
        await self._request({"op": "cancel", "task_id": task_id})

    async def is_cancelled(self, task_id: str) -> bool:
        resp = await self._request({"op": "is_cancelled", "task_id": task_id})
        return bool(resp.get("data", False))

    async def send_progress(self, task_id: str, progress_json: str) -> None:
        await self._request({
            "op": "progress_put", "task_id": task_id, "data": progress_json,
        })

    async def get_progress(self, task_id: str) -> str | None:
        resp = await self._request({"op": "progress_get", "task_id": task_id})
        return resp.get("data")

    async def notify_result(self, task_id: str) -> None:
        pass  # broker dispatches notifications inside result_put

    async def subscribe_results(self) -> AsyncIterator[str]:
        reader, writer = await asyncio.open_connection(self._host, self._port)
        try:
            await _send_msg(writer, {"op": "subscribe"})
            while True:
                msg = await _recv_msg(reader)
                yield msg["data"]
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            return
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await self._writer.wait_closed()
            self._reader = self._writer = None
        self._kill_broker()
