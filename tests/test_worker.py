from __future__ import annotations

import asyncio
import json

import pytest

from pyfuse.core.errors import WorkerError
from pyfuse.core.models import FunctionNode, ImportInfo
from pyfuse.graph.store import Store
from pyfuse.core.task import Task
from pyfuse.worker.worker import BuildInfo, Worker, _compute_subgraph_key, execute


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


# -- Worker.run ------------------------------------------------------

class TestRun:
    @pytest.mark.asyncio
    async def test_simple_function(self) -> None:
        _, json_str = _simple_store()
        task = Task(graph_json=json_str, function_name="f", args=(21,))
        worker = Worker(auto_install=False)
        assert await worker.run(task) == 42

    @pytest.mark.asyncio
    async def test_with_dependencies(self) -> None:
        _, json_str = _chain_store()
        task = Task(graph_json=json_str, function_name="a", args=(5,))
        worker = Worker(auto_install=False)
        assert await worker.run(task) == 16  # b(5) + 1 = 15 + 1 = 16

    @pytest.mark.asyncio
    async def test_class_method(self) -> None:
        _, json_str = _class_store()
        store = Store.from_json(json_str)
        source = store.reconstruct("greet")
        ns: dict = {}
        exec(compile(source, "<test>", "exec"), ns)
        instance = ns["Greeter"]()
        task = Task(graph_json=json_str, function_name="greet", args=(instance, "world"))
        worker = Worker(auto_install=False)
        assert await worker.run(task) == "hello world"

    @pytest.mark.asyncio
    async def test_function_not_found(self) -> None:
        _, json_str = _simple_store()
        task = Task(graph_json=json_str, function_name="nonexistent")
        worker = Worker(auto_install=False)
        with pytest.raises(KeyError):
            await worker.run(task)

    @pytest.mark.asyncio
    async def test_with_kwargs(self) -> None:
        _, json_str = _simple_store()
        task = Task(graph_json=json_str, function_name="f", kwargs={"x": 10})
        worker = Worker(auto_install=False)
        assert await worker.run(task) == 20

    @pytest.mark.asyncio
    async def test_async_function(self) -> None:
        _, json_str = _async_store()
        task = Task(graph_json=json_str, function_name="af", args=(5,))
        worker = Worker(auto_install=False)
        assert await worker.run(task) == 15

    @pytest.mark.asyncio
    async def test_uses_cache(self) -> None:
        _, json_str = _simple_store()
        task = Task(graph_json=json_str, function_name="f", args=(1,))
        worker = Worker(auto_install=False)
        await worker.run(task)
        assert worker.cache_info()["size"] == 1
        await worker.run(task)
        assert worker.cache_info()["size"] == 1


# -- Caching -----------------------------------------------------------------

class TestCaching:
    @pytest.mark.asyncio
    async def test_cache_hit(self) -> None:
        _, json_str = _simple_store()
        worker = Worker(auto_install=False)
        task = Task(graph_json=json_str, function_name="f", args=(1,))
        await worker.run(task)
        assert worker.cache_info()["size"] == 1
        task2 = Task(graph_json=json_str, function_name="f", args=(2,))
        await worker.run(task2)
        assert worker.cache_info()["size"] == 1

    @pytest.mark.asyncio
    async def test_cache_miss_different_graph(self) -> None:
        _, json1 = _simple_store()
        _, json2 = _chain_store()
        worker = Worker(auto_install=False)
        await worker.run(Task(graph_json=json1, function_name="f", args=(1,)))
        await worker.run(Task(graph_json=json2, function_name="a", args=(1,)))
        assert worker.cache_info()["size"] == 2

    @pytest.mark.asyncio
    async def test_clear_cache(self) -> None:
        _, json_str = _simple_store()
        worker = Worker(auto_install=False)
        await worker.run(Task(graph_json=json_str, function_name="f", args=(1,)))
        assert worker.cache_info()["size"] == 1
        worker.clear_cache()
        assert worker.cache_info()["size"] == 0


# -- run_with_policy --------------------------------------------------------

class TestRunWithPolicy:
    @pytest.mark.asyncio
    async def test_simple(self) -> None:
        _, json_str = _simple_store()
        task = Task(graph_json=json_str, function_name="f", args=(21,))
        worker = Worker(auto_install=False)
        assert await worker.run_with_policy(task) == 42

    @pytest.mark.asyncio
    async def test_with_timeout(self) -> None:
        _, json_str = _simple_store()
        task = Task(graph_json=json_str, function_name="f", args=(21,), timeout=5.0)
        worker = Worker(auto_install=False)
        assert await worker.run_with_policy(task) == 42

    @pytest.mark.asyncio
    async def test_async_function(self) -> None:
        _, json_str = _async_store()
        task = Task(graph_json=json_str, function_name="af", args=(5,))
        worker = Worker(auto_install=False)
        assert await worker.run_with_policy(task) == 15

    @pytest.mark.asyncio
    async def test_async_with_timeout(self) -> None:
        _, json_str = _async_store()
        task = Task(graph_json=json_str, function_name="af", args=(5,), timeout=5.0)
        worker = Worker(auto_install=False)
        assert await worker.run_with_policy(task) == 15


# -- BuildInfo tracking -------------------------------------------------------

class TestBuildInfo:
    @pytest.mark.asyncio
    async def test_initial_none(self) -> None:
        worker = Worker(auto_install=False)
        assert worker.last_build_info() is None

    @pytest.mark.asyncio
    async def test_cache_miss(self) -> None:
        _, json_str = _simple_store()
        worker = Worker(auto_install=False)
        await worker.run(Task(graph_json=json_str, function_name="f", args=(1,)))
        info = worker.last_build_info()
        assert info is not None
        assert info.cache_hit is False
        assert info.installed_packages == []

    @pytest.mark.asyncio
    async def test_cache_hit(self) -> None:
        _, json_str = _simple_store()
        worker = Worker(auto_install=False)
        await worker.run(Task(graph_json=json_str, function_name="f", args=(1,)))
        await worker.run(Task(graph_json=json_str, function_name="f", args=(2,)))
        info = worker.last_build_info()
        assert info is not None
        assert info.cache_hit is True

    @pytest.mark.asyncio
    async def test_run_tracks_build_info(self) -> None:
        _, json_str = _simple_store()
        task = Task(graph_json=json_str, function_name="f", args=(5,))
        worker = Worker(auto_install=False)
        await worker.run(task)
        info = worker.last_build_info()
        assert info is not None
        assert info.cache_hit is False


# -- Convenience function ---------------------------------------------------

class TestConvenienceExecute:
    @pytest.mark.asyncio
    async def test_execute_function(self) -> None:
        _, json_str = _simple_store()
        result = await execute(json_str, "f", 7)
        assert result == 14

    @pytest.mark.asyncio
    async def test_execute_with_task(self) -> None:
        _, json_str = _simple_store()
        task = Task(graph_json=json_str, function_name="f", args=(7,))
        result = await execute(task)
        assert result == 14

    @pytest.mark.asyncio
    async def test_execute_task_ignores_extra_args(self) -> None:
        _, json_str = _simple_store()
        task = Task(graph_json=json_str, function_name="f", args=(7,))
        result = await execute(task)
        assert result == 14

    @pytest.mark.asyncio
    async def test_execute_str_without_function_name_raises(self) -> None:
        _, json_str = _simple_store()
        with pytest.raises(TypeError, match="function_name is required"):
            await execute(json_str)
