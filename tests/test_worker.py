from __future__ import annotations

import asyncio
import json

import pytest

from pyfuse.core.errors import WorkerError
from pyfuse.core.models import FunctionNode, ImportInfo
from pyfuse.graph.store import Store
from pyfuse.core.task import Task
from pyfuse.worker.worker import Worker, _compute_subgraph_key, execute


# -- Helpers -----------------------------------------------------------------

def _node(
    name: str,
    source: str | None = None,
    imports: list[ImportInfo] | None = None,
    owner_class: str | None = None,
) -> FunctionNode:
    return FunctionNode(
        qualified_name=f"m.{name}",
        name=name,
        module="m",
        source=source or f"def {name}():\n    pass\n",
        imports=imports or [],
        dependencies=[],
        owner_class=owner_class,
        closure_vars={},
        closure_func_refs={},
    )


def _simple_store() -> tuple[Store, str]:
    """Store with a single function that returns 42."""
    node = _node("f", source="def f(x):\n    return x * 2\n")
    store = Store()
    h = store.put(node)
    store.set_ref("m.f", h)
    return store, store.to_json()


def _chain_store() -> tuple[Store, str]:
    """Store with A -> B chain."""
    a = _node("a", source="def a(x):\n    return b(x) + 1\n")
    b = _node("b", source="def b(x):\n    return x * 3\n")
    store = Store()
    ha, hb = store.put(a), store.put(b)
    store.set_ref("m.a", ha)
    store.set_ref("m.b", hb)
    store.set_deps(ha, [hb])
    return store, store.to_json()


def _class_store() -> tuple[Store, str]:
    """Store with a class method."""
    method = _node(
        "greet",
        source="def greet(self, name):\n    return f'hello {name}'\n",
        owner_class="m.Greeter",
    )
    store = Store()
    h = store.put(method)
    store.set_ref("m.Greeter.greet", h)
    return store, store.to_json()


def _async_store() -> tuple[Store, str]:
    """Store with an async function."""
    node = _node("af", source="async def af(x):\n    return x + 10\n")
    store = Store()
    h = store.put(node)
    store.set_ref("m.af", h)
    return store, store.to_json()


# -- _compute_subgraph_key --------------------------------------------------

class TestSubgraphKey:
    def test_deterministic(self) -> None:
        store, _ = _simple_store()
        k1 = _compute_subgraph_key(store, "f")
        k2 = _compute_subgraph_key(store, "f")
        assert k1 == k2

    def test_different_for_different_graphs(self) -> None:
        s1, _ = _simple_store()
        s2, _ = _chain_store()
        k1 = _compute_subgraph_key(s1, "f")
        k2 = _compute_subgraph_key(s2, "a")
        assert k1 != k2

    def test_length(self) -> None:
        store, _ = _simple_store()
        key = _compute_subgraph_key(store, "f")
        assert len(key) == 16


# -- Worker.execute ------------------------------------------------------

class TestExecute:
    def test_simple_function(self) -> None:
        _, json_str = _simple_store()
        worker = Worker(auto_install=False)
        result = worker.execute(json_str, "f", 21)
        assert result == 42

    def test_with_dependencies(self) -> None:
        _, json_str = _chain_store()
        worker = Worker(auto_install=False)
        result = worker.execute(json_str, "a", 5)
        assert result == 16  # b(5) + 1 = 15 + 1 = 16

    def test_class_method(self) -> None:
        _, json_str = _class_store()
        worker = Worker(auto_install=False)
        # Extract the class, instantiate, then call method via unbound
        store = Store.from_json(json_str)
        source = store.reconstruct("greet")
        ns: dict = {}
        exec(compile(source, "<test>", "exec"), ns)
        instance = ns["Greeter"]()
        result = worker.execute(json_str, "greet", instance, "world")
        assert result == "hello world"

    def test_function_not_found(self) -> None:
        _, json_str = _simple_store()
        worker = Worker(auto_install=False)
        with pytest.raises(KeyError):
            worker.execute(json_str, "nonexistent")

    def test_kwargs(self) -> None:
        _, json_str = _simple_store()
        worker = Worker(auto_install=False)
        result = worker.execute(json_str, "f", x=10)
        assert result == 20


# -- Caching -----------------------------------------------------------------

class TestCaching:
    def test_cache_hit(self) -> None:
        _, json_str = _simple_store()
        worker = Worker(auto_install=False)
        worker.execute(json_str, "f", 1)
        info = worker.cache_info()
        assert info["size"] == 1
        worker.execute(json_str, "f", 2)
        assert worker.cache_info()["size"] == 1

    def test_cache_miss_different_graph(self) -> None:
        _, json1 = _simple_store()
        _, json2 = _chain_store()
        worker = Worker(auto_install=False)
        worker.execute(json1, "f", 1)
        worker.execute(json2, "a", 1)
        assert worker.cache_info()["size"] == 2

    def test_clear_cache(self) -> None:
        _, json_str = _simple_store()
        worker = Worker(auto_install=False)
        worker.execute(json_str, "f", 1)
        assert worker.cache_info()["size"] == 1
        worker.clear_cache()
        assert worker.cache_info()["size"] == 0


# -- Async execution --------------------------------------------------------

class TestAsyncExecute:
    def test_async_function(self) -> None:
        _, json_str = _async_store()
        worker = Worker(auto_install=False)
        result = asyncio.run(worker.execute_async(json_str, "af", 5))
        assert result == 15


# -- Convenience function ---------------------------------------------------

class TestRun:
    def test_run_simple(self) -> None:
        _, json_str = _simple_store()
        task = Task(graph_json=json_str, function_name="f", args=(21,))
        worker = Worker(auto_install=False)
        assert worker.run(task) == 42

    def test_run_with_kwargs(self) -> None:
        _, json_str = _simple_store()
        task = Task(graph_json=json_str, function_name="f", kwargs={"x": 10})
        worker = Worker(auto_install=False)
        assert worker.run(task) == 20

    def test_run_with_dependencies(self) -> None:
        _, json_str = _chain_store()
        task = Task(graph_json=json_str, function_name="a", args=(5,))
        worker = Worker(auto_install=False)
        assert worker.run(task) == 16

    def test_run_async(self) -> None:
        _, json_str = _async_store()
        task = Task(graph_json=json_str, function_name="af", args=(5,))
        worker = Worker(auto_install=False)
        result = asyncio.run(worker.run_async(task))
        assert result == 15

    def test_run_uses_cache(self) -> None:
        _, json_str = _simple_store()
        task = Task(graph_json=json_str, function_name="f", args=(1,))
        worker = Worker(auto_install=False)
        worker.run(task)
        assert worker.cache_info()["size"] == 1
        worker.run(task)
        assert worker.cache_info()["size"] == 1


class TestConvenienceExecute:
    def test_execute_function(self) -> None:
        _, json_str = _simple_store()
        result = execute(json_str, "f", 7)
        assert result == 14

    def test_execute_with_task(self) -> None:
        _, json_str = _simple_store()
        task = Task(graph_json=json_str, function_name="f", args=(7,))
        result = execute(task)
        assert result == 14

    def test_execute_task_ignores_extra_args(self) -> None:
        _, json_str = _simple_store()
        task = Task(graph_json=json_str, function_name="f", args=(7,))
        result = execute(task)
        assert result == 14

    def test_execute_str_without_function_name_raises(self) -> None:
        _, json_str = _simple_store()
        with pytest.raises(TypeError, match="function_name is required"):
            execute(json_str)
