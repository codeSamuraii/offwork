"""Tests for the top-level offwork API additions: get_graph(), graph, pack()."""

import warnings

import pytest

import offwork
from offwork import Graph, Task, get_graph, pack


# -- get_graph() / graph ----------------------------------------------------

class TestGraph:
    def test_get_graph_returns_default(self) -> None:
        g = get_graph()
        assert isinstance(g, Graph)
        assert g is Graph.default()

    def test_graph_subpackage_accessible(self) -> None:
        assert hasattr(offwork.graph, 'Graph')
        assert offwork.graph.Graph is Graph


# -- pack() -----------------------------------------------------------------

class TestPack:
    def _make_traced(self):
        """Create traced functions within the current default graph."""
        @offwork.task
        def double(x: int) -> int:
            return x * 2

        @offwork.task
        def add_then_double(a: int, b: int) -> int:
            return double(a + b)

        return double, add_then_double

    def test_returns_task(self) -> None:
        double, _ = self._make_traced()
        task = pack(double, 5)
        assert isinstance(task, Task)

    def test_captures_function_name(self) -> None:
        double, _ = self._make_traced()
        task = pack(double, 5)
        assert "double" in task.function_name

    def test_captures_args(self) -> None:
        double, _ = self._make_traced()
        task = pack(double, 5)
        assert task.args == (5,)

    def test_captures_kwargs(self) -> None:
        double, _ = self._make_traced()
        task = pack(double, x=5)
        assert task.kwargs == {"x": 5}

    def test_graph_is_scoped_subgraph(self) -> None:
        double, _ = self._make_traced()
        task = pack(double, 1)
        assert "double" in task.graph_json
        assert "add_then_double" not in task.graph_json

    @pytest.mark.asyncio
    async def test_task_is_executable(self) -> None:
        double, _ = self._make_traced()
        task = pack(double, 21)
        result = await offwork.execute(task)
        assert result == 42

    def test_auto_generated_task_id(self) -> None:
        double, _ = self._make_traced()
        t1 = pack(double, 1)
        t2 = pack(double, 1)
        assert t1.task_id != t2.task_id

    @pytest.mark.asyncio
    async def test_roundtrip_through_json(self) -> None:
        _, add_then_double = self._make_traced()
        task = pack(add_then_double, 3, 4)
        restored = Task.from_json(task.to_json())
        result = await offwork.execute(restored)
        assert result == 14  # (3 + 4) * 2
