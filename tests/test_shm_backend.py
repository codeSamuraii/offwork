"""Tests for the SharedMemoryBackend (same-machine IPC via shared memory)."""
from __future__ import annotations

import socket
import threading
from collections.abc import Iterator

import pytest

from pyfuse.worker.backends.shm import (
    SharedMemoryBackend,
    _read_shm_block,
    _write_shm_block,
)
import pyfuse.worker.remote as _remote


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def _clean_backend() -> Iterator[None]:
    yield
    _remote._active_backend = None


@pytest.fixture
def shm_backend() -> Iterator[SharedMemoryBackend]:
    port = _free_port()
    backend = SharedMemoryBackend(f"shm://localhost:{port}", server=True)
    yield backend
    backend.close()


# ---------------------------------------------------------------------------
# Low-level shm block helpers
# ---------------------------------------------------------------------------


class TestShmBlocks:
    def test_roundtrip(self) -> None:
        payload = '{"hello": "world"}'
        name = _write_shm_block(payload)
        assert _read_shm_block(name) == payload

    def test_empty_string(self) -> None:
        name = _write_shm_block("")
        assert _read_shm_block(name) == ""

    def test_unicode(self) -> None:
        payload = "caf\u00e9 \U0001f680"
        name = _write_shm_block(payload)
        assert _read_shm_block(name) == payload

    def test_large_payload(self) -> None:
        payload = "x" * (1024 * 1024)  # 1 MB
        name = _write_shm_block(payload)
        assert _read_shm_block(name) == payload


# ---------------------------------------------------------------------------
# Backend contract
# ---------------------------------------------------------------------------


class TestSharedMemoryBackend:
    def test_submit_and_listen(self, shm_backend: SharedMemoryBackend) -> None:
        shm_backend.submit('{"test": 1}')
        shm_backend.submit('{"test": 2}')

        results: list[str] = []
        for task_json in shm_backend.listen():
            results.append(task_json)
            if len(results) == 2:
                break

        assert results == ['{"test": 1}', '{"test": 2}']

    def test_send_and_get_result(self, shm_backend: SharedMemoryBackend) -> None:
        shm_backend.send_result("t1", '{"ok": true}')
        assert shm_backend.get_result("t1") == '{"ok": true}'

    def test_try_get_result_none(self, shm_backend: SharedMemoryBackend) -> None:
        assert shm_backend.try_get_result("missing") is None

    def test_try_get_result_success(self, shm_backend: SharedMemoryBackend) -> None:
        shm_backend.send_result("t1", '{"ok": true}')
        assert shm_backend.try_get_result("t1") == '{"ok": true}'

    def test_get_result_timeout(self, shm_backend: SharedMemoryBackend) -> None:
        with pytest.raises(TimeoutError):
            shm_backend.get_result("missing", timeout=0.1)

    def test_large_task_roundtrip(self, shm_backend: SharedMemoryBackend) -> None:
        payload = '{"data": "' + "a" * 100_000 + '"}'
        shm_backend.submit(payload)
        for task_json in shm_backend.listen():
            assert task_json == payload
            break

    def test_close(self, shm_backend: SharedMemoryBackend) -> None:
        shm_backend.close()
        # After close, the broker is gone — no crash, just None
        assert shm_backend._broker is None


# ---------------------------------------------------------------------------
# Client connects to existing server
# ---------------------------------------------------------------------------


class TestClientServer:
    def test_client_connects_to_server(self) -> None:
        port = _free_port()
        server = SharedMemoryBackend(f"shm://localhost:{port}", server=True)
        try:
            client = SharedMemoryBackend(f"shm://localhost:{port}", server=False)
            try:
                client.submit('{"from_client": true}')
                for task_json in server.listen():
                    assert task_json == '{"from_client": true}'
                    break
            finally:
                client.close()
        finally:
            server.close()

    def test_result_flows_back(self) -> None:
        port = _free_port()
        server = SharedMemoryBackend(f"shm://localhost:{port}", server=True)
        try:
            client = SharedMemoryBackend(f"shm://localhost:{port}", server=False)
            try:
                server.send_result("t42", '{"value": 42}')
                assert client.get_result("t42") == '{"value": 42}'
            finally:
                client.close()
        finally:
            server.close()


# ---------------------------------------------------------------------------
# Integration with connect()
# ---------------------------------------------------------------------------


class TestConnectDispatch:
    def test_connect_shm(self) -> None:
        port = _free_port()
        backend = _remote.connect(f"shm://localhost:{port}")
        try:
            assert isinstance(backend, SharedMemoryBackend)
            assert _remote._active_backend is backend
        finally:
            _remote.disconnect()


# ---------------------------------------------------------------------------
# End-to-end with worker
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_submit_execute_result(self) -> None:
        """Full cycle: submit task on shm backend, execute in worker thread, read result."""
        from pyfuse import pack, trace
        from pyfuse.worker.result import ResultEnvelope
        from pyfuse.core.task import Task
        from pyfuse.worker.worker import Worker

        @trace
        def add(a: int, b: int) -> int:
            return a + b

        port = _free_port()
        server = SharedMemoryBackend(f"shm://localhost:{port}", server=True)
        client = SharedMemoryBackend(f"shm://localhost:{port}", server=False)

        try:
            task = pack(add, 3, 4)
            client.submit(task.to_json())

            # Worker side: pop one task, execute, push result
            worker = Worker(auto_install=False)
            for task_json in server.listen():
                t = Task.from_json(task_json)
                try:
                    result = worker.run(t)
                    env = ResultEnvelope.success(t.task_id, result)
                except Exception as exc:
                    env = ResultEnvelope.failure(t.task_id, exc)
                server.send_result(t.task_id, env.to_json())
                break

            raw = client.get_result(task.task_id)
            env = ResultEnvelope.from_json(raw)
            assert env.status == "ok"
            assert env.result == 7
        finally:
            client.close()
            server.close()

    def test_worker_thread(self) -> None:
        """Worker running in a background thread processes tasks submitted from main thread."""
        from pyfuse import pack, trace
        from pyfuse.worker.result import ResultEnvelope
        from pyfuse.core.task import Task
        from pyfuse.worker.worker import Worker

        @trace
        def multiply(a: int, b: int) -> int:
            return a * b

        port = _free_port()
        server = SharedMemoryBackend(f"shm://localhost:{port}", server=True)
        client = SharedMemoryBackend(f"shm://localhost:{port}", server=False)

        worker_done = threading.Event()

        def worker_loop() -> None:
            worker = Worker(auto_install=False)
            for task_json in server.listen():
                t = Task.from_json(task_json)
                try:
                    result = worker.run(t)
                    env = ResultEnvelope.success(t.task_id, result)
                except Exception as exc:
                    env = ResultEnvelope.failure(t.task_id, exc)
                server.send_result(t.task_id, env.to_json())
                worker_done.set()
                break

        thread = threading.Thread(target=worker_loop, daemon=True)
        thread.start()

        try:
            task = pack(multiply, 6, 7)
            client.submit(task.to_json())

            worker_done.wait(timeout=10)
            raw = client.get_result(task.task_id, timeout=5)
            env = ResultEnvelope.from_json(raw)
            assert env.status == "ok"
            assert env.result == 42
        finally:
            client.close()
            server.close()
            thread.join(timeout=5)
