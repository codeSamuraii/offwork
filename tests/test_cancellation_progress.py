"""Tests for task cancellation and progress reporting."""
from __future__ import annotations

import asyncio
import collections
import json
import time
from typing import Any

import pytest

from pyfuse.core.errors import TaskCancelled
from pyfuse.core.models import FunctionNode, ImportInfo
from pyfuse.core.progress import ProgressInfo, _progress_callback, progress
from pyfuse.core.task import Task
from pyfuse.graph.store import Store
from pyfuse.worker import remote as _remote
from pyfuse.worker.backends.base import Backend
from pyfuse.worker.remote import _handle_task
from pyfuse.worker.result import Result, ResultEnvelope
from pyfuse.worker.worker import Worker


# ---------------------------------------------------------------------------
# In-memory backend with cancel + progress support
# ---------------------------------------------------------------------------


class InMemoryBackend(Backend):
    """Minimal in-memory backend for testing cancel and progress."""

    def __init__(self) -> None:
        self.tasks: collections.deque[str] = collections.deque()
        self.results: dict[str, str] = {}
        self.heartbeats: dict[str, float] = {}
        self.cancelled: set[str] = set()
        self.progress_data: dict[str, str] = {}
        self.stop = False

    async def submit(self, task_json: str) -> None:
        self.tasks.append(task_json)

    async def listen(self) -> Any:
        while not self.stop:
            if self.tasks:
                yield self.tasks.popleft()
            else:
                await asyncio.sleep(0.01)

    async def send_result(self, task_id: str, result_json: str) -> None:
        self.results[task_id] = result_json

    async def get_result(self, task_id: str, timeout: float | None = None) -> str:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if task_id in self.results:
                return self.results.pop(task_id)
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError
            await asyncio.sleep(0.01)

    async def try_get_result(self, task_id: str) -> str | None:
        return self.results.pop(task_id, None)

    async def send_heartbeat(self, task_id: str) -> None:
        self.heartbeats[task_id] = time.time()

    async def get_heartbeat(self, task_id: str) -> float | None:
        return self.heartbeats.get(task_id)

    async def cancel_task(self, task_id: str) -> None:
        self.cancelled.add(task_id)

    async def is_cancelled(self, task_id: str) -> bool:
        return task_id in self.cancelled

    async def send_progress(self, task_id: str, progress_json: str) -> None:
        self.progress_data[task_id] = progress_json

    async def get_progress(self, task_id: str) -> str | None:
        return self.progress_data.get(task_id)

    async def close(self) -> None:
        self.stop = True


# ---------------------------------------------------------------------------
# Store helpers
# ---------------------------------------------------------------------------


def _node(name: str, source: str) -> FunctionNode:
    return FunctionNode(
        qualified_name=f"m.{name}",
        name=name,
        module="m",
        source=source,
        imports=[],
        dependencies=[],
    )


def _sync_store() -> tuple[Store, str]:
    store = Store()
    node = _node("f", "def f(x):\n    return x * 2")
    h = store.put(node)
    store.set_ref("f", h)
    return store, store.to_json()


def _slow_store() -> tuple[Store, str]:
    store = Store()
    node = _node("slow", (
        "def slow(n):\n"
        "    import time\n"
        "    total = 0\n"
        "    for i in range(n):\n"
        "        time.sleep(0.1)\n"
        "        total += i\n"
        "    return total"
    ))
    node.imports = [ImportInfo(statement="import time", bound_name="time")]
    h = store.put(node)
    store.set_ref("slow", h)
    return store, store.to_json()


def _async_slow_store() -> tuple[Store, str]:
    store = Store()
    node = _node("slow_async", (
        "async def slow_async(n):\n"
        "    import asyncio\n"
        "    total = 0\n"
        "    for i in range(n):\n"
        "        await asyncio.sleep(0.1)\n"
        "        total += i\n"
        "    return total"
    ))
    h = store.put(node)
    store.set_ref("slow_async", h)
    return store, store.to_json()


def _progress_store() -> tuple[Store, str]:
    store = Store()
    node = _node("process", (
        "def process(items):\n"
        "    from pyfuse import progress\n"
        "    results = []\n"
        "    for i, item in enumerate(items):\n"
        "        results.append(item.upper())\n"
        "        progress(i + 1, len(items), message=f'item {i+1}')\n"
        "    return results"
    ))
    node.imports = [ImportInfo(
        statement="from pyfuse import progress",
        bound_name="progress",
    )]
    h = store.put(node)
    store.set_ref("process", h)
    return store, store.to_json()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _clean_backend() -> Any:
    yield
    _remote._active_backend = None
    _remote._atexit_registered = False


@pytest.fixture
def backend() -> InMemoryBackend:
    return InMemoryBackend()


# ---------------------------------------------------------------------------
# ResultEnvelope cancelled
# ---------------------------------------------------------------------------


class TestResultEnvelopeCancelled:
    def test_cancelled_envelope(self) -> None:
        env = ResultEnvelope.cancelled("t1")
        assert env.status == "cancelled"
        assert env.task_id == "t1"
        assert env.result is None

    def test_cancelled_roundtrip(self) -> None:
        env = ResultEnvelope.cancelled("t1")
        raw = env.to_json()
        restored = ResultEnvelope.from_json(raw)
        assert restored.status == "cancelled"
        assert restored.task_id == "t1"

    def test_cancelled_json_no_error_fields(self) -> None:
        env = ResultEnvelope.cancelled("t1")
        data = json.loads(env.to_json())
        assert "error_type" not in data
        assert "error_message" not in data


# ---------------------------------------------------------------------------
# Task Cancellation
# ---------------------------------------------------------------------------


class TestCancellation:
    @pytest.mark.asyncio
    async def test_cancel_sets_flag(self, backend: InMemoryBackend) -> None:
        result = Result("task1", backend)
        await result.cancel()
        assert await backend.is_cancelled("task1")

    @pytest.mark.asyncio
    async def test_cancel_stores_result(self, backend: InMemoryBackend) -> None:
        result = Result("task1", backend)
        await result.cancel()
        raw = await backend.try_get_result("task1")
        assert raw is not None
        env = ResultEnvelope.from_json(raw)
        assert env.status == "cancelled"

    @pytest.mark.asyncio
    async def test_await_cancelled_raises(self, backend: InMemoryBackend) -> None:
        result = Result("task1", backend)
        await result.cancel()
        with pytest.raises(TaskCancelled, match="task1"):
            await result

    @pytest.mark.asyncio
    async def test_status_returns_cancelled(self, backend: InMemoryBackend) -> None:
        result = Result("task1", backend)
        await result.cancel()
        assert await result.status() == "cancelled"

    @pytest.mark.asyncio
    async def test_handle_task_skips_cancelled(self, backend: InMemoryBackend) -> None:
        """Worker skips execution for already-cancelled tasks."""
        _, json_str = _sync_store()
        task = Task(graph_json=json_str, function_name="f", args=(5,))

        # Cancel before the worker handles it
        await backend.cancel_task(task.task_id)

        worker = Worker(auto_install=False)
        await _handle_task(worker, backend, task.to_json())

        # Worker should not have sent a result (the cancel already stored one)
        assert task.task_id not in backend.results

    @pytest.mark.asyncio
    async def test_cancel_during_async_execution(
        self, backend: InMemoryBackend, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cancelling during async execution interrupts the function."""
        monkeypatch.setattr(_remote, "_HEARTBEAT_INTERVAL", 0.1)

        _, json_str = _async_slow_store()
        # 30 iterations × 0.1 s = 3 s without cancellation
        task = Task(graph_json=json_str, function_name="slow_async", args=(30,))

        worker = Worker(auto_install=False)

        async def cancel_later() -> None:
            await asyncio.sleep(0.15)
            await backend.cancel_task(task.task_id)

        t0 = time.monotonic()
        asyncio.create_task(cancel_later())
        await _handle_task(worker, backend, task.to_json())
        elapsed = time.monotonic() - t0

        # Should finish well before 3 s (the function was interrupted)
        assert elapsed < 1.5

        # Worker sends a cancelled envelope
        assert task.task_id in backend.results
        env = ResultEnvelope.from_json(backend.results[task.task_id])
        assert env.status == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_during_sync_execution(
        self, backend: InMemoryBackend, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cancelling during sync execution marks result as cancelled."""
        monkeypatch.setattr(_remote, "_HEARTBEAT_INTERVAL", 0.1)

        _, json_str = _slow_store()
        task = Task(graph_json=json_str, function_name="slow", args=(5,))

        worker = Worker(auto_install=False)

        async def cancel_later() -> None:
            await asyncio.sleep(0.15)
            await backend.cancel_task(task.task_id)

        asyncio.create_task(cancel_later())
        await _handle_task(worker, backend, task.to_json())

        # Executor thread can't be interrupted, but the coroutine is cancelled
        assert task.task_id in backend.results
        env = ResultEnvelope.from_json(backend.results[task.task_id])
        assert env.status == "cancelled"


# ---------------------------------------------------------------------------
# Progress Reporting
# ---------------------------------------------------------------------------


class TestProgressInfo:
    def test_percent_with_total(self) -> None:
        info = ProgressInfo(current=5, total=10)
        assert info.percent == 50.0

    def test_percent_without_total(self) -> None:
        info = ProgressInfo(current=5)
        assert info.percent is None

    def test_percent_zero_total(self) -> None:
        info = ProgressInfo(current=0, total=0)
        assert info.percent is None

    def test_roundtrip_json(self) -> None:
        info = ProgressInfo(current=3, total=10, message="step 3")
        restored = ProgressInfo.from_json(info.to_json())
        assert restored.current == 3
        assert restored.total == 10
        assert restored.message == "step 3"

    def test_json_minimal(self) -> None:
        info = ProgressInfo(current=1)
        data = json.loads(info.to_json())
        assert data == {"current": 1}


class TestProgressFunction:
    def test_noop_without_context(self) -> None:
        """progress() is a no-op when not inside a worker."""
        progress(1, 10, message="test")  # Should not raise

    def test_calls_callback_current_total(self) -> None:
        calls: list[tuple[float, float | None, str | None]] = []

        def cb(current: float, total: float | None, message: str | None) -> None:
            calls.append((current, total, message))

        token = _progress_callback.set(cb)
        try:
            progress(1, 10, message="step 1")
            progress(2, 10, message="step 2")
        finally:
            _progress_callback.reset(token)

        assert calls == [(1, 10, "step 1"), (2, 10, "step 2")]

    def test_calls_callback_percent(self) -> None:
        calls: list[tuple[float, float | None, str | None]] = []

        def cb(current: float, total: float | None, message: str | None) -> None:
            calls.append((current, total, message))

        token = _progress_callback.set(cb)
        try:
            progress(50.0)
            progress(75.5, message="three quarters")
        finally:
            _progress_callback.reset(token)

        assert calls == [(50.0, 100, None), (75.5, 100, "three quarters")]


class TestResultProgress:
    @pytest.mark.asyncio
    async def test_no_progress(self, backend: InMemoryBackend) -> None:
        result = Result("task1", backend)
        assert await result.progress() is None

    @pytest.mark.asyncio
    async def test_get_progress(self, backend: InMemoryBackend) -> None:
        info = ProgressInfo(current=5, total=10, message="halfway")
        await backend.send_progress("task1", info.to_json())

        result = Result("task1", backend)
        p = await result.progress()
        assert p is not None
        assert p.current == 5
        assert p.total == 10
        assert p.message == "halfway"

    @pytest.mark.asyncio
    async def test_progress_updates(self, backend: InMemoryBackend) -> None:
        result = Result("task1", backend)

        await backend.send_progress("task1", ProgressInfo(current=1, total=3).to_json())
        p = await result.progress()
        assert p is not None
        assert p.current == 1

        await backend.send_progress("task1", ProgressInfo(current=3, total=3).to_json())
        p = await result.progress()
        assert p is not None
        assert p.current == 3


class TestProgressInjection:
    @pytest.mark.asyncio
    async def test_handle_task_injects_progress(self, backend: InMemoryBackend) -> None:
        """_handle_task sets up the progress context for executed functions."""
        _, json_str = _progress_store()
        task = Task(
            graph_json=json_str,
            function_name="process",
            args=(["a", "b", "c"],),
        )

        worker = Worker(auto_install=False)
        await _handle_task(worker, backend, task.to_json())

        # flush() guarantees final progress is delivered -- no sleep needed
        raw = await backend.get_progress(task.task_id)
        assert raw is not None
        info = ProgressInfo.from_json(raw)
        assert info.current == 3
        assert info.total == 3

        # And the result should be correct
        raw_result = await backend.try_get_result(task.task_id)
        assert raw_result is not None
        env = ResultEnvelope.from_json(raw_result)
        assert env.status == "ok"
        assert env.result == ["A", "B", "C"]


# ---------------------------------------------------------------------------
# Backend base defaults
# ---------------------------------------------------------------------------


class TestBackendDefaults:
    @pytest.mark.asyncio
    async def test_cancel_noop(self) -> None:
        """Base Backend.cancel_task is a no-op."""

        class Minimal(Backend):
            async def submit(self, task_json: str) -> None: ...
            def listen(self) -> Any: ...
            async def send_result(self, task_id: str, result_json: str) -> None: ...
            async def get_result(self, task_id: str, timeout: float | None = None) -> str:
                return ""
            async def try_get_result(self, task_id: str) -> str | None:
                return None
            async def close(self) -> None: ...

        b = Minimal()
        await b.cancel_task("t1")
        assert await b.is_cancelled("t1") is False

    @pytest.mark.asyncio
    async def test_progress_noop(self) -> None:
        """Base Backend.send_progress/get_progress are no-ops."""

        class Minimal(Backend):
            async def submit(self, task_json: str) -> None: ...
            def listen(self) -> Any: ...
            async def send_result(self, task_id: str, result_json: str) -> None: ...
            async def get_result(self, task_id: str, timeout: float | None = None) -> str:
                return ""
            async def try_get_result(self, task_id: str) -> str | None:
                return None
            async def close(self) -> None: ...

        b = Minimal()
        await b.send_progress("t1", '{"current": 1}')
        assert await b.get_progress("t1") is None
