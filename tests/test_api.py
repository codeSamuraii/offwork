"""Tests for the top-level pyfuse API additions: graph(), pack(), analyze() deprecation."""
from __future__ import annotations

import warnings

import pyfuse
from pyfuse import FuseGraph, Task, graph, pack, trace


# -- graph() ----------------------------------------------------------------

class TestGraph:
    def test_returns_default_graph(self) -> None:
        g = graph()
        assert isinstance(g, FuseGraph)
        assert g is FuseGraph.default()


# -- pack() -----------------------------------------------------------------

class TestPack:
    def _make_traced(self):
        """Create traced functions within the current default graph."""
        @trace
        def double(x: int) -> int:
            return x * 2

        @trace
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

    def test_task_is_executable(self) -> None:
        double, _ = self._make_traced()
        task = pack(double, 21)
        result = pyfuse.execute(task)
        assert result == 42

    def test_auto_generated_task_id(self) -> None:
        double, _ = self._make_traced()
        t1 = pack(double, 1)
        t2 = pack(double, 1)
        assert t1.task_id != t2.task_id

    def test_roundtrip_through_json(self) -> None:
        _, add_then_double = self._make_traced()
        task = pack(add_then_double, 3, 4)
        restored = Task.from_json(task.to_json())
        result = pyfuse.execute(restored)
        assert result == 14  # (3 + 4) * 2
