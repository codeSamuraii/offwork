"""Tests for the LocalBackend (async-native TCP backend)."""
from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator

import pytest

from pyfuse.worker.backends.local import LocalBackend
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
async def backend() -> AsyncIterator[LocalBackend]:
    port = _free_port()
    b = LocalBackend(f"local://127.0.0.1:{port}", server=True)
    yield b
    await b.close()


# ---------------------------------------------------------------------------
# Backend contract
# ---------------------------------------------------------------------------


class TestLocalBackend:
    @pytest.mark.asyncio
    async def test_submit_and_listen(self, backend: LocalBackend) -> None:
        await backend.submit('{"test": 1}')
        await backend.submit('{"test": 2}')

        results: list[str] = []
        async for task_json in backend.listen():
            results.append(task_json)
            if len(results) == 2:
                break

        assert results == ['{"test": 1}', '{"test": 2}']

    @pytest.mark.asyncio
    async def test_send_and_get_result(self, backend: LocalBackend) -> None:
        await backend.send_result("t1", '{"ok": true}')
        assert await backend.get_result("t1") == '{"ok": true}'

    @pytest.mark.asyncio
    async def test_try_get_result_none(self, backend: LocalBackend) -> None:
        assert await backend.try_get_result("missing") is None

    @pytest.mark.asyncio
    async def test_try_get_result_success(self, backend: LocalBackend) -> None:
        await backend.send_result("t1", '{"ok": true}')
        assert await backend.try_get_result("t1") == '{"ok": true}'

    @pytest.mark.asyncio
    async def test_get_result_timeout(self, backend: LocalBackend) -> None:
        with pytest.raises(TimeoutError):
            await backend.get_result("missing", timeout=0.2)

    @pytest.mark.asyncio
    async def test_large_task_roundtrip(self, backend: LocalBackend) -> None:
        payload = '{"data": "' + "a" * 100_000 + '"}'
        await backend.submit(payload)
        async for task_json in backend.listen():
            assert task_json == payload
            break

    @pytest.mark.asyncio
    async def test_heartbeat(self, backend: LocalBackend) -> None:
        assert await backend.get_heartbeat("t1") is None
        await backend.send_heartbeat("t1")
        hb = await backend.get_heartbeat("t1")
        assert hb is not None and hb > 0

    @pytest.mark.asyncio
    async def test_heartbeats_batch(self, backend: LocalBackend) -> None:
        await backend.send_heartbeat("t1")
        result = await backend.get_heartbeats(["t1", "t2"])
        assert result["t1"] is not None
        assert result["t2"] is None

    @pytest.mark.asyncio
    async def test_close(self, backend: LocalBackend) -> None:
        await backend.close()
        assert backend._writer is None


# ---------------------------------------------------------------------------
# Client connects to existing broker
# ---------------------------------------------------------------------------


class TestClientServer:
    @pytest.mark.asyncio
    async def test_client_connects_to_broker(self) -> None:
        port = _free_port()
        server = LocalBackend(f"local://127.0.0.1:{port}", server=True)
        try:
            client = LocalBackend(f"local://127.0.0.1:{port}", server=False)
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
        server = LocalBackend(f"local://127.0.0.1:{port}", server=True)
        try:
            client = LocalBackend(f"local://127.0.0.1:{port}", server=False)
            try:
                await server.send_result("t42", '{"value": 42}')
                raw = await client.get_result("t42")
                assert raw == '{"value": 42}'
            finally:
                await client.close()
        finally:
            await server.close()


# ---------------------------------------------------------------------------
# Integration with connect()
# ---------------------------------------------------------------------------


class TestConnectDispatch:
    @pytest.mark.asyncio
    async def test_connect_local(self) -> None:
        port = _free_port()
        backend = _remote.connect(f"local://127.0.0.1:{port}")
        try:
            assert isinstance(backend, LocalBackend)
            assert _remote._active_backend is backend
        finally:
            await _remote.disconnect()


# ---------------------------------------------------------------------------
# End-to-end with worker
# ---------------------------------------------------------------------------


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_submit_execute_result(self) -> None:
        """Full cycle: submit on local backend, execute in worker, read result."""
        from pyfuse import pack, trace
        from pyfuse.worker.result import ResultEnvelope
        from pyfuse.core.task import Task
        from pyfuse.worker.worker import Worker

        @trace
        def add(a: int, b: int) -> int:
            return a + b

        port = _free_port()
        server = LocalBackend(f"local://127.0.0.1:{port}", server=True)
        client = LocalBackend(f"local://127.0.0.1:{port}", server=False)

        try:
            task = pack(add, 3, 4)
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

            raw = await client.get_result(task.task_id)
            env = ResultEnvelope.from_json(raw)
            assert env.status == "ok"
            assert env.result == 7
        finally:
            await client.close()
            await server.close()

    @pytest.mark.asyncio
    async def test_concurrent_tasks(self) -> None:
        """Multiple tasks processed concurrently."""
        from pyfuse import pack, trace
        from pyfuse.worker.result import ResultEnvelope
        from pyfuse.core.task import Task
        from pyfuse.worker.worker import Worker

        @trace
        def double(n: int) -> int:
            return n * 2

        port = _free_port()
        server = LocalBackend(f"local://127.0.0.1:{port}", server=True)
        client = LocalBackend(f"local://127.0.0.1:{port}", server=False)

        try:
            tasks = [pack(double, i) for i in range(5)]
            for task in tasks:
                await client.submit(task.to_json())

            worker = Worker(auto_install=False)
            count = 0
            async for task_json in server.listen():
                t = Task.from_json(task_json)
                try:
                    result = await worker.run(t)
                    env = ResultEnvelope.success(t.task_id, result)
                except Exception as exc:
                    env = ResultEnvelope.failure(t.task_id, exc)
                await server.send_result(t.task_id, env.to_json())
                count += 1
                if count == 5:
                    break

            for i, task in enumerate(tasks):
                raw = await client.get_result(task.task_id)
                env = ResultEnvelope.from_json(raw)
                assert env.status == "ok"
                assert env.result == i * 2
        finally:
            await client.close()
            await server.close()
