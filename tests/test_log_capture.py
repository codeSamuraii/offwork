"""Wide tests for per-task log capture (worker → backend send_log_line)."""

from __future__ import annotations

import asyncio
import collections
import logging
import time
from typing import Any

import pytest

from offwork.core.models import FunctionNode
from offwork.core.task import Task
from offwork.graph.store import Store
from offwork.worker.backends.base import Backend
from offwork.worker.remote import _handle_task
from offwork.worker.worker import Worker


class _LogBackend(Backend):
    """In-memory backend that records log lines per task."""

    def __init__(self) -> None:
        self.tasks: collections.deque[str] = collections.deque()
        self.results: dict[str, str] = {}
        self.log_lines: dict[str, list[str]] = {}
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

    async def send_log_line(self, task_id: str, line: str) -> None:
        self.log_lines.setdefault(task_id, []).append(line)

    async def close(self) -> None:
        self.stop = True


def _logging_task_store() -> tuple[Store, str]:
    store = Store()
    node = FunctionNode(
        qualified_name="m.run",
        name="run",
        module="m",
        source=(
            "def run():\n"
            "    import logging\n"
            "    logging.getLogger('offwork.test').info('hello from worker')\n"
            "    return 42\n"
        ),
        imports=[],
        dependencies=[],
    )
    h = store.put(node)
    store.set_ref("run", h)
    return store, store.to_json()


@pytest.mark.asyncio
async def test_handle_task_ships_log_lines_to_backend() -> None:
    """Task logging records reach the backend via send_log_line."""
    logging.getLogger().setLevel(logging.INFO)
    backend = _LogBackend()
    store, graph_json = _logging_task_store()
    task = Task(graph_json=graph_json, function_name="run")
    worker = Worker(auto_install=False)

    await backend.submit(task.to_json())
    async for task_json in backend.listen():
        await _handle_task(worker, backend, task_json)
        break

    lines = backend.log_lines.get(task.task_id, [])
    assert any("hello from worker" in line for line in lines), (
        f"expected worker log line in {lines!r}"
    )
