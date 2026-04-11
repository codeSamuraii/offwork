"""Shared-memory backend for same-machine IPC.

Uses ``multiprocessing.shared_memory`` for zero-copy payload transfer
and ``multiprocessing.managers.BaseManager`` for cross-process coordination.
"""
from __future__ import annotations

import atexit
import logging
import queue
import struct
import threading
import time
import uuid
from collections.abc import Iterator
from multiprocessing.managers import BaseManager
from multiprocessing.shared_memory import SharedMemory
from typing import Any
from urllib.parse import parse_qs, urlparse

from pyfuse.worker.backends.base import Backend

logger = logging.getLogger(__name__)

_SHM_HEADER_SIZE = 8  # uint64 payload length


# ---------------------------------------------------------------------------
# Shared memory block I/O
# ---------------------------------------------------------------------------


def _write_shm_block(payload: str) -> str:
    """Write *payload* into a new ``SharedMemory`` block. Returns the block name."""
    data = payload.encode("utf-8")
    name = f"pf_{uuid.uuid4().hex[:12]}"
    # track=False: cleanup is handled by _ShmBlockTracker and the consumer's
    # unlink(), so we don't need the resource_tracker (which would warn on
    # exit about blocks already unlinked by the consumer).
    block = SharedMemory(create=True, size=_SHM_HEADER_SIZE + len(data), name=name, track=False)
    try:
        buf = block.buf
        assert buf is not None
        struct.pack_into("Q", buf, 0, len(data))
        buf[_SHM_HEADER_SIZE : _SHM_HEADER_SIZE + len(data)] = data
    finally:
        block.close()
    return name


def _read_shm_block(name: str, *, unlink: bool = True) -> str:
    """Read a string payload from a ``SharedMemory`` block.

    Unlinks the block by default (consumer cleans up).
    """
    block = SharedMemory(name=name, create=False, track=False)
    try:
        buf = block.buf
        assert buf is not None
        length = struct.unpack_from("Q", buf, 0)[0]
        data = bytes(buf[_SHM_HEADER_SIZE : _SHM_HEADER_SIZE + length])
        return data.decode("utf-8")
    finally:
        block.close()
        if unlink:
            block.unlink()


# ---------------------------------------------------------------------------
# Block tracker for crash-safe cleanup
# ---------------------------------------------------------------------------


class _ShmBlockTracker:
    """Tracks shm blocks created by this process for cleanup."""

    def __init__(self) -> None:
        self._blocks: set[str] = set()
        self._lock = threading.Lock()

    def register(self, name: str) -> None:
        with self._lock:
            self._blocks.add(name)

    def unregister(self, name: str) -> None:
        with self._lock:
            self._blocks.discard(name)

    def cleanup(self) -> None:
        with self._lock:
            for name in list(self._blocks):
                try:
                    block = SharedMemory(name=name, create=False, track=False)
                    block.close()
                    block.unlink()
                except FileNotFoundError:
                    pass
            self._blocks.clear()


# ---------------------------------------------------------------------------
# Broker (lives inside the Manager server)
# ---------------------------------------------------------------------------


class _ShmBroker:
    """Coordination broker managing task queue and per-task result slots."""

    def __init__(self) -> None:
        self._task_queue: queue.Queue[str] = queue.Queue()
        self._result_queues: dict[str, queue.Queue[str]] = {}
        self._lock = threading.Lock()

    def put_task(self, shm_name: str) -> None:
        self._task_queue.put(shm_name)

    def get_task(self, timeout: float | None = None) -> str | None:
        try:
            return self._task_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def put_result(self, task_id: str, shm_name: str) -> None:
        with self._lock:
            if task_id not in self._result_queues:
                self._result_queues[task_id] = queue.Queue(maxsize=1)
        self._result_queues[task_id].put(shm_name)

    def get_result(self, task_id: str, timeout: float | None = None) -> str | None:
        with self._lock:
            if task_id not in self._result_queues:
                self._result_queues[task_id] = queue.Queue(maxsize=1)
            q = self._result_queues[task_id]
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            return None

    def try_get_result(self, task_id: str) -> str | None:
        with self._lock:
            q = self._result_queues.get(task_id)
            if q is None:
                return None
        try:
            return q.get_nowait()
        except queue.Empty:
            return None


# ---------------------------------------------------------------------------
# Manager subclasses
# ---------------------------------------------------------------------------

_broker_instance: _ShmBroker | None = None
_broker_lock = threading.Lock()


def _get_broker() -> _ShmBroker:
    global _broker_instance
    with _broker_lock:
        if _broker_instance is None:
            _broker_instance = _ShmBroker()
        return _broker_instance


class _PyfuseManager(BaseManager):
    pass


class _PyfuseManagerClient(BaseManager):
    pass


_PyfuseManager.register("broker", callable=_get_broker)
_PyfuseManagerClient.register("broker")


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 9847
_DEFAULT_AUTHKEY = b"pyfuse"


def _parse_shm_url(url: str) -> tuple[str, int, bytes]:
    """Parse ``shm://host:port?authkey=...`` into (host, port, authkey)."""
    parsed = urlparse(url)
    host = parsed.hostname or _DEFAULT_HOST
    port = parsed.port or _DEFAULT_PORT
    qs = parse_qs(parsed.query)
    authkey_list = qs.get("authkey", [])
    authkey = authkey_list[0].encode() if authkey_list else _DEFAULT_AUTHKEY
    return host, port, authkey


# ---------------------------------------------------------------------------
# SharedMemoryBackend
# ---------------------------------------------------------------------------


class SharedMemoryBackend(Backend):
    """Shared-memory transport for same-machine IPC.

    Uses ``multiprocessing.shared_memory`` for payload data and a
    ``multiprocessing.managers.BaseManager`` for coordination (task queue
    and result routing).

    Parameters
    ----------
    url
        Connection URL, e.g. ``shm://localhost:9847``.
    authkey
        Authentication key for the Manager server. Defaults to ``b"pyfuse"``.
    server
        If ``True``, start the Manager server.  If ``False``, connect as a
        client only.  If ``None`` (default), try to connect first; if that
        fails, start a server automatically.
    """

    def __init__(
        self,
        url: str = "shm://localhost",
        *,
        authkey: bytes | str | None = None,
        server: bool | None = None,
    ) -> None:
        host, port, auth = _parse_shm_url(url)
        if authkey is not None:
            auth = authkey.encode() if isinstance(authkey, str) else authkey

        self._is_server = False
        self._server_manager: _PyfuseManager | None = None
        self._client: _PyfuseManagerClient | None = None
        self._broker: Any = None
        self._tracker = _ShmBlockTracker()
        atexit.register(self._tracker.cleanup)

        if server is True:
            self._start_server(host, port, auth)
        elif server is False:
            self._connect_client(host, port, auth)
        else:
            try:
                self._connect_client(host, port, auth)
            except (ConnectionRefusedError, OSError):
                try:
                    self._start_server(host, port, auth)
                except OSError:
                    # Another process won the race — retry as client
                    time.sleep(0.1)
                    self._connect_client(host, port, auth)

        assert self._client is not None
        self._broker = self._client.broker()
        logger.info(
            "SharedMemoryBackend ready (server=%s, %s:%d)",
            self._is_server, host, port,
        )

    # -- Backend interface ------------------------------------------------------

    def submit(self, task_json: str) -> None:
        name = _write_shm_block(task_json)
        self._tracker.register(name)
        self._broker.put_task(name)

    def listen(self) -> Iterator[str]:
        while True:
            name = self._broker.get_task(timeout=1.0)
            if name is None:
                continue
            self._tracker.unregister(name)
            yield _read_shm_block(name)

    def send_result(self, task_id: str, result_json: str) -> None:
        name = _write_shm_block(result_json)
        self._tracker.register(name)
        self._broker.put_result(task_id, name)

    def get_result(self, task_id: str, timeout: float | None = None) -> str:
        name = self._broker.get_result(task_id, timeout=timeout)
        if name is None:
            raise TimeoutError(
                f"Timed out waiting for result of task {task_id}"
            )
        self._tracker.unregister(name)
        return _read_shm_block(name)

    def try_get_result(self, task_id: str) -> str | None:
        name = self._broker.try_get_result(task_id)
        if name is None:
            return None
        self._tracker.unregister(name)
        return _read_shm_block(name)

    def close(self) -> None:
        self._tracker.cleanup()
        if self._is_server and self._server_manager is not None:
            self._server_manager.shutdown()
            self._server_manager = None
            # Reset the module-level broker so a fresh server can start later
            global _broker_instance
            with _broker_lock:
                _broker_instance = None
        self._client = None
        self._broker = None

    # -- internals --------------------------------------------------------------

    def _start_server(self, host: str, port: int, authkey: bytes) -> None:
        mgr = _PyfuseManager(address=(host, port), authkey=authkey)
        mgr.start()
        self._is_server = True
        self._server_manager = mgr
        # Connect as a client to our own server
        self._connect_client(host, port, authkey)

    def _connect_client(self, host: str, port: int, authkey: bytes) -> None:
        client = _PyfuseManagerClient(address=(host, port), authkey=authkey)
        client.connect()
        self._client = client
