"""Tests for async features: Result.result, __await__, .run(), .map(),
async worker execution, and heartbeat-based stall detection."""

import asyncio
import collections
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest

from seeya import pack, trace
from seeya.core.errors import RemoteError, TaskStalled
from seeya.core.models import FunctionNode, ImportInfo
from seeya.core.task import Task
from seeya.graph.store import Store
from seeya.worker.backends.base import Backend
from seeya.worker.result import Result, ResultEnvelope
from seeya.worker.worker import Worker
import seeya.worker.remote as _remote


# ---------------------------------------------------------------------------
# InMemoryBackend with heartbeat support (async)
# ---------------------------------------------------------------------------


class InMemoryBackend(Backend):
    def __init__(self) -> None:
        self._tasks: collections.deque[str] = collections.deque()
        self._results: dict[str, collections.deque[str]] = {}
        self._heartbeats: dict[str, float] = {}
        self._stop = False

    async def submit(self, task_json: str) -> None:
        self._tasks.append(task_json)

    async def listen(self) -> AsyncIterator[str]:  # type: ignore[override]
        while not self._stop:
            if self._tasks:
                yield self._tasks.popleft()
            else:
                break

    async def send_result(self, task_id: str, result_json: str) -> None:
        self._results.setdefault(task_id, collections.deque()).append(result_json)

    async def get_result(self, task_id: str, timeout: float | None = None) -> str:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            q = self._results.get(task_id)
            if q:
                return q.popleft()
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"No result for {task_id}")
            if deadline is None:
                raise TimeoutError(f"No result for {task_id}")
            await asyncio.sleep(0.05)

    async def try_get_result(self, task_id: str) -> str | None:
        q = self._results.get(task_id)
        if q:
            return q.popleft()
        return None

    async def send_heartbeat(self, task_id: str) -> None:
        self._heartbeats[task_id] = time.time()

    async def get_heartbeat(self, task_id: str) -> float | None:
        return self._heartbeats.get(task_id)

    async def get_heartbeats(self, task_ids: list[str]) -> dict[str, float | None]:
        return {tid: self._heartbeats.get(tid) for tid in task_ids}

    async def close(self) -> None:
        self._stop = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _node(
    name: str,
    source: str | None = None,
    imports: list[ImportInfo] | None = None,
) -> FunctionNode:
    return FunctionNode(
        qualified_name=f"m.{name}",
        name=name,
        module="m",
        source=source or f"def {name}():\n    pass\n",
        imports=imports or [],
        dependencies=[],
        owner_class=None,
        closure_vars={},
        closure_func_refs={},
    )


def _async_store() -> tuple[Store, str]:
    """Store with an async function."""
    node = _node("af", source="async def af(x):\n    return x + 10\n")
    store = Store()
    h = store.put(node)
    store.set_ref("m.af", h)
    return store, store.to_json()


def _sync_store() -> tuple[Store, str]:
    """Store with a sync function."""
    node = _node("f", source="def f(x):\n    return x * 2\n")
    store = Store()
    h = store.put(node)
    store.set_ref("m.f", h)
    return store, store.to_json()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _clean_backend() -> AsyncIterator[None]:
    yield
    _remote._active_backend = None
    _remote._atexit_registered = False


@pytest.fixture
def backend() -> InMemoryBackend:
    return InMemoryBackend()


# ---------------------------------------------------------------------------
# Result.result (async)
# ---------------------------------------------------------------------------


class TestResultAsync:
    @pytest.mark.asyncio
    async def test_result_success(self, backend: InMemoryBackend) -> None:
        env = ResultEnvelope.success("t1", 42)
        await backend.send_result("t1", env.to_json())

        future = Result("t1", backend)
        value = await future.result(stall_timeout=None)
        assert value == 42

    @pytest.mark.asyncio
    async def test_result_raises_on_error(self, backend: InMemoryBackend) -> None:
        env = ResultEnvelope.failure("t2", ValueError("bad"))
        await backend.send_result("t2", env.to_json())

        future = Result("t2", backend)
        with pytest.raises(RemoteError, match="ValueError: bad"):
            await future.result(stall_timeout=None)

    @pytest.mark.asyncio
    async def test_result_uses_cached_envelope(self, backend: InMemoryBackend) -> None:
        env = ResultEnvelope.success("t3", "cached")
        await backend.send_result("t3", env.to_json())

        future = Result("t3", backend)
        assert await future.result(stall_timeout=None) == "cached"
        # Second call uses cached envelope (queue is now empty)
        assert await future.result(stall_timeout=None) == "cached"

    @pytest.mark.asyncio
    async def test_result_timeout(self, backend: InMemoryBackend) -> None:
        future = Result("missing", backend)
        with pytest.raises(TimeoutError):
            await future.result(timeout=0, stall_timeout=None)


# ---------------------------------------------------------------------------
# Result.__await__
# ---------------------------------------------------------------------------


class TestResultAwait:
    @pytest.mark.asyncio
    async def test_await_result(self, backend: InMemoryBackend) -> None:
        env = ResultEnvelope.success("t1", 77)
        await backend.send_result("t1", env.to_json())

        future = Result("t1", backend)
        value = await future
        assert value == 77

    @pytest.mark.asyncio
    async def test_await_raises_on_error(self, backend: InMemoryBackend) -> None:
        env = ResultEnvelope.failure("t2", RuntimeError("boom"))
        await backend.send_result("t2", env.to_json())

        future = Result("t2", backend)
        with pytest.raises(RemoteError, match="RuntimeError: boom"):
            await future


# ---------------------------------------------------------------------------
# Worker: async function execution via run()
# ---------------------------------------------------------------------------


class TestWorkerAsyncRun:
    @pytest.mark.asyncio
    async def test_run_async_function_via_task(self) -> None:
        """Worker.run() transparently handles async def functions."""
        _, json_str = _async_store()
        task = Task(graph_json=json_str, function_name="af", args=(5,))
        worker = Worker(auto_install=False)
        result = await worker.run(task)
        assert result == 15

    @pytest.mark.asyncio
    async def test_run_sync_function(self) -> None:
        """Sync functions still work normally via run()."""
        _, json_str = _sync_store()
        task = Task(graph_json=json_str, function_name="f", args=(21,))
        worker = Worker(auto_install=False)
        assert await worker.run(task) == 42

    @pytest.mark.asyncio
    async def test_run_with_policy_async(self) -> None:
        """run_with_policy handles async functions via run()."""
        _, json_str = _async_store()
        task = Task(graph_json=json_str, function_name="af", args=(5,))
        worker = Worker(auto_install=False)
        result = await worker.run_with_policy(task)
        assert result == 15

    @pytest.mark.asyncio
    async def test_run_with_policy_timeout_async(self) -> None:
        """run_with_policy with timeout handles async functions."""
        _, json_str = _async_store()
        task = Task(
            graph_json=json_str, function_name="af", args=(5,), timeout=5.0
        )
        worker = Worker(auto_install=False)
        result = await worker.run_with_policy(task)
        assert result == 15


# ---------------------------------------------------------------------------
# _handle_task with async functions
# ---------------------------------------------------------------------------


class TestHandleTaskAsync:
    @pytest.mark.asyncio
    async def test_handle_async_task(self, backend: InMemoryBackend) -> None:
        """_handle_task correctly processes async functions."""

        @trace
        async def async_double(x: int) -> int:
            return x * 2

        task = pack(async_double, 7)
        await backend.submit(task.to_json())

        worker = Worker(auto_install=False)
        async for task_json in backend.listen():
            await _remote._handle_task(worker, backend, task_json)

        raw = await backend.get_result(task.task_id)
        env = ResultEnvelope.from_json(raw)
        assert env.status == "ok"
        assert env.result == 14


# ---------------------------------------------------------------------------
# .run() on traced functions (async submit)
# ---------------------------------------------------------------------------


class TestStartMethod:
    @pytest.mark.asyncio
    async def test_start_exists(self) -> None:
        @trace
        def my_func(x: int) -> int:
            return x + 1

        assert hasattr(my_func, "start")
        assert asyncio.iscoroutinefunction(my_func.start)

    @pytest.mark.asyncio
    async def test_start_submits_and_returns_result(self, backend: InMemoryBackend) -> None:
        _remote._active_backend = backend

        @trace
        def double(x: int) -> int:
            return x * 2

        future = await double.start(5)
        assert isinstance(future, Result)
        assert len(backend._tasks) == 1

        task_data = json.loads(backend._tasks[0])
        task_id = task_data["id"]
        env = ResultEnvelope.success(task_id, 10)
        await backend.send_result(task_id, env.to_json())

        result = await future.result(stall_timeout=None)
        assert result == 10

    @pytest.mark.asyncio
    async def test_start_on_async_func(self, backend: InMemoryBackend) -> None:
        _remote._active_backend = backend

        @trace
        async def async_triple(x: int) -> int:
            return x * 3

        assert hasattr(async_triple, "start")
        assert asyncio.iscoroutinefunction(async_triple.start)


class TestRunMethod:
    @pytest.mark.asyncio
    async def test_run_exists(self) -> None:
        @trace
        def my_func(x: int) -> int:
            return x + 1

        assert hasattr(my_func, "run")
        assert asyncio.iscoroutinefunction(my_func.run)


# ---------------------------------------------------------------------------
# .map() on traced functions (async batch submit)
# ---------------------------------------------------------------------------


class TestMapMethod:
    @pytest.mark.asyncio
    async def test_map_exists(self) -> None:
        @trace
        def my_func(x: int) -> int:
            return x + 1

        assert hasattr(my_func, "map")
        assert asyncio.iscoroutinefunction(my_func.map)


# ---------------------------------------------------------------------------
# asyncio.gather with Result.__await__
# ---------------------------------------------------------------------------


class TestGather:
    @pytest.mark.asyncio
    async def test_gather_multiple_results(self, backend: InMemoryBackend) -> None:
        _remote._active_backend = backend

        @trace
        def square(x: int) -> int:
            return x ** 2

        f1 = await square.start(3)
        f2 = await square.start(4)

        # Simulate worker responses
        for task_json in list(backend._tasks):
            task_data = json.loads(task_json)
            task_id = task_data["id"]
            val = task_data["args"][0] ** 2
            env = ResultEnvelope.success(task_id, val)
            await backend.send_result(task_id, env.to_json())

        r1, r2 = await asyncio.gather(f1, f2)
        assert r1 == 9
        assert r2 == 16


# ---------------------------------------------------------------------------
# Heartbeat: worker side
# ---------------------------------------------------------------------------


class TestWorkerHeartbeat:
    @pytest.mark.asyncio
    async def test_handle_task_sends_heartbeats(self, backend: InMemoryBackend) -> None:
        """_handle_task sends heartbeats while executing."""

        @trace
        def slow_add(a: int, b: int) -> int:
            import time as _t
            _t.sleep(0.3)
            return a + b

        task = pack(slow_add, 1, 2)
        await backend.submit(task.to_json())

        worker = Worker(auto_install=False)
        async for task_json in backend.listen():
            await _remote._handle_task(worker, backend, task_json)

        # Worker should have sent heartbeats
        assert task.task_id in backend._heartbeats

        # Result should still be correct
        raw = await backend.get_result(task.task_id)
        env = ResultEnvelope.from_json(raw)
        assert env.status == "ok"
        assert env.result == 3

    @pytest.mark.asyncio
    async def test_heartbeat_stops_after_completion(
        self, backend: InMemoryBackend
    ) -> None:
        """Heartbeat task stops once the task completes."""

        @trace
        def quick() -> int:
            return 42

        task = pack(quick)
        await backend.submit(task.to_json())

        worker = Worker(auto_install=False)
        async for task_json in backend.listen():
            await _remote._handle_task(worker, backend, task_json)

        # Record timestamp right after completion
        hb_after = backend._heartbeats.get(task.task_id)

        # Wait a bit -- heartbeat should NOT update further
        await asyncio.sleep(0.2)
        hb_later = backend._heartbeats.get(task.task_id)
        assert hb_after == hb_later


# ---------------------------------------------------------------------------
# Heartbeat: stall detection (async)
# ---------------------------------------------------------------------------


class TestStallDetection:
    @pytest.mark.asyncio
    async def test_no_stall_when_heartbeating(self, backend: InMemoryBackend) -> None:
        """result() doesn't raise when heartbeats keep arriving."""
        future = Result("t1", backend)

        async def worker_sim() -> None:
            for _ in range(3):
                await backend.send_heartbeat("t1")
                await asyncio.sleep(0.05)
            env = ResultEnvelope.success("t1", 42)
            await backend.send_result("t1", env.to_json())

        asyncio.create_task(worker_sim())
        value = await future.result(stall_timeout=1.0)
        assert value == 42

    @pytest.mark.asyncio
    async def test_stall_detected_when_heartbeat_stops(
        self, backend: InMemoryBackend
    ) -> None:
        """result() raises TaskStalled when heartbeats stop."""
        future = Result("t2", backend)

        await backend.send_heartbeat("t2")
        await asyncio.sleep(0.01)

        with pytest.raises(TaskStalled, match="stalled"):
            await future.result(stall_timeout=0.2)

    @pytest.mark.asyncio
    async def test_no_stall_without_any_heartbeat(
        self, backend: InMemoryBackend
    ) -> None:
        """No stall raised if no heartbeat was ever seen (worker hasn't started)."""
        future = Result("t3", backend)

        async def deliver() -> None:
            await asyncio.sleep(0.15)
            env = ResultEnvelope.success("t3", 99)
            await backend.send_result("t3", env.to_json())

        asyncio.create_task(deliver())
        # stall_timeout is very short but no heartbeat was ever seen,
        # so stall detection shouldn't trigger
        value = await future.result(timeout=1.0, stall_timeout=0.05)
        assert value == 99

    @pytest.mark.asyncio
    async def test_stall_timeout_none_disables(
        self, backend: InMemoryBackend
    ) -> None:
        """stall_timeout=None disables stall detection."""
        future = Result("t4", backend)

        await backend.send_heartbeat("t4")

        # Deliver result after stall would have triggered
        async def deliver() -> None:
            await asyncio.sleep(0.3)
            env = ResultEnvelope.success("t4", 77)
            await backend.send_result("t4", env.to_json())

        asyncio.create_task(deliver())
        value = await future.result(timeout=1.0, stall_timeout=None)
        assert value == 77

    @pytest.mark.asyncio
    async def test_stall_includes_task_id(self, backend: InMemoryBackend) -> None:
        """TaskStalled message includes the task_id."""
        future = Result("my_task_99", backend)

        await backend.send_heartbeat("my_task_99")
        await asyncio.sleep(0.01)

        with pytest.raises(TaskStalled, match="my_task_99"):
            await future.result(stall_timeout=0.15)


# ---------------------------------------------------------------------------
# Backend heartbeat defaults
# ---------------------------------------------------------------------------


class TestBackendHeartbeatDefaults:
    @pytest.mark.asyncio
    async def test_base_send_heartbeat_is_noop(self) -> None:
        """Base Backend.send_heartbeat is a no-op (no error)."""

        class MinimalBackend(Backend):
            async def submit(self, task_json: str) -> None: ...
            async def listen(self) -> AsyncIterator[str]:  # type: ignore[override]
                return
                yield  # type: ignore[misc]
            async def send_result(self, task_id: str, result_json: str) -> None: ...
            async def get_result(self, task_id: str, timeout: float | None = None) -> str:
                raise TimeoutError
            async def try_get_result(self, task_id: str) -> str | None:
                return None
            async def close(self) -> None: ...

        b = MinimalBackend()
        await b.send_heartbeat("any_task")  # should not raise
        assert await b.get_heartbeat("any_task") is None
