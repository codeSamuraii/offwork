"""Tests for the SharedMemoryBackend (same-machine IPC via shared memory)."""
from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator

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
async def _clean_backend() -> AsyncIterator[None]:
    yield
    _remote._active_backend = None


@pytest.fixture
async def shm_backend() -> AsyncIterator[SharedMemoryBackend]:
    port = _free_port()
    backend = SharedMemoryBackend(f"shm://localhost:{port}", server=True)
    yield backend
    await backend.close()


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
    @pytest.mark.asyncio
    async def test_submit_and_listen(self, shm_backend: SharedMemoryBackend) -> None:
        await shm_backend.submit('{"test": 1}')
        await shm_backend.submit('{"test": 2}')

        results: list[str] = []
        async for task_json in shm_backend.listen():
            results.append(task_json)
            if len(results) == 2:
                break

        assert results == ['{"test": 1}', '{"test": 2}']

    @pytest.mark.asyncio
    async def test_send_and_get_result(self, shm_backend: SharedMemoryBackend) -> None:
        await shm_backend.send_result("t1", '{"ok": true}')
        assert await shm_backend.get_result("t1") == '{"ok": true}'

    @pytest.mark.asyncio
    async def test_try_get_result_none(self, shm_backend: SharedMemoryBackend) -> None:
        assert await shm_backend.try_get_result("missing") is None

    @pytest.mark.asyncio
    async def test_try_get_result_success(self, shm_backend: SharedMemoryBackend) -> None:
        await shm_backend.send_result("t1", '{"ok": true}')
        assert await shm_backend.try_get_result("t1") == '{"ok": true}'

    @pytest.mark.asyncio
    async def test_get_result_timeout(self, shm_backend: SharedMemoryBackend) -> None:
        with pytest.raises(TimeoutError):
            await shm_backend.get_result("missing", timeout=0.1)

    @pytest.mark.asyncio
    async def test_large_task_roundtrip(self, shm_backend: SharedMemoryBackend) -> None:
        payload = '{"data": "' + "a" * 100_000 + '"}'
        await shm_backend.submit(payload)
        async for task_json in shm_backend.listen():
            assert task_json == payload
            break

    @pytest.mark.asyncio
    async def test_close(self, shm_backend: SharedMemoryBackend) -> None:
        await shm_backend.close()
        # After close, the broker is gone -- no crash, just None
        assert shm_backend._broker is None


# ---------------------------------------------------------------------------
# Client connects to existing server
# ---------------------------------------------------------------------------


class TestClientServer:
    @pytest.mark.asyncio
    async def test_client_connects_to_server(self) -> None:
        port = _free_port()
        server = SharedMemoryBackend(f"shm://localhost:{port}", server=True)
        try:
            client = SharedMemoryBackend(f"shm://localhost:{port}", server=False)
            try:
                await client.submit('{"from_client": true}')
                async for task_json in server.listen():
                    assert task_json == '{"from_client": true}'
                    break
            finally:
                await client.close()
        finally:
            await server.close()

    @pytest.mark.asyncio
    async def test_result_flows_back(self) -> None:
        port = _free_port()
        server = SharedMemoryBackend(f"shm://localhost:{port}", server=True)
        try:
            client = SharedMemoryBackend(f"shm://localhost:{port}", server=False)
            try:
                await server.send_result("t42", '{"value": 42}')
                assert await client.get_result("t42") == '{"value": 42}'
            finally:
                await client.close()
        finally:
            await server.close()


# ---------------------------------------------------------------------------
# Integration with connect()
# ---------------------------------------------------------------------------


class TestConnectDispatch:
    @pytest.mark.asyncio
    async def test_connect_shm(self) -> None:
        port = _free_port()
        backend = _remote.connect(f"shm://localhost:{port}")
        try:
            assert isinstance(backend, SharedMemoryBackend)
            assert _remote._active_backend is backend
        finally:
            await _remote.disconnect()


# ---------------------------------------------------------------------------
# End-to-end with worker
# ---------------------------------------------------------------------------


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_submit_execute_result(self) -> None:
        """Full cycle: submit task on shm backend, execute in worker, read result."""
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
            await client.submit(task.to_json())

            # Worker side: pop one task, execute, push result
            worker = Worker(auto_install=False)
            async for task_json in server.listen():
                t = Task.from_json(task_json)
                try:
                    result = await worker.run(t)
                    env = ResultEnvelope.success(t.task_id, result)
                except Exception as exc:
                    env = ResultEnvelope.failure(t.task_id, exc)
                await server.send_result(t.task_id, env.to_json())
                break

            raw = await client.get_result(task.task_id)
            env = ResultEnvelope.from_json(raw)
            assert env.status == "ok"
            assert env.result == 7
        finally:
            await client.close()
            await server.close()

    @pytest.mark.asyncio
    async def test_worker_processes_tasks(self) -> None:
        """Worker processes tasks submitted via shm backend."""
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

        try:
            task = pack(multiply, 6, 7)
            await client.submit(task.to_json())

            worker = Worker(auto_install=False)
            async for task_json in server.listen():
                t = Task.from_json(task_json)
                try:
                    result = await worker.run(t)
                    env = ResultEnvelope.success(t.task_id, result)
                except Exception as exc:
                    env = ResultEnvelope.failure(t.task_id, exc)
                await server.send_result(t.task_id, env.to_json())
                break

            raw = await client.get_result(task.task_id, timeout=5)
            env = ResultEnvelope.from_json(raw)
            assert env.status == "ok"
            assert env.result == 42
        finally:
            await client.close()
            await server.close()
