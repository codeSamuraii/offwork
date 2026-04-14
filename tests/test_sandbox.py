"""Tests for the sandbox subsystem.

These tests exercise:
  - NoopExecutor (local exec, default behaviour)
  - DockerExecutor (container-based sandbox)
  - Guest agent protocol and execution logic
  - SandboxConfig and create_executor factory
  - Worker integration with sandbox

The VMExecutor tests that require a running tart VM are skipped in CI
(no Apple Silicon + Virtualization.framework available).
DockerExecutor tests that require a running Docker daemon are also
included (unit tests run without Docker; integration would need it).
"""

import asyncio
import json
import struct

import pytest

from pyfuse.core.errors import WorkerError
from pyfuse.core.task import Task
from pyfuse.worker.sandbox import SandboxExecutor, SandboxConfig, NoopExecutor, create_executor
from pyfuse.worker.sandbox._protocol import encode, decode_header, HEADER_SIZE, async_send, async_recv
from pyfuse.worker.sandbox.guest_agent import _execute_request
from pyfuse.worker.worker import Worker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_source(body: str) -> str:
    """Wrap a function body into a proper def."""
    return f"def f(x):\n    return {body}\n"


def _make_store_json(source: str, name: str = "f", module: str = "m") -> str:
    """Build a minimal Store JSON string for a single function."""
    from pyfuse.core.models import FunctionNode, ImportInfo
    from pyfuse.graph.store import Store

    node = FunctionNode(
        qualified_name=f"{module}.{name}",
        name=name,
        module=module,
        source=source,
        imports=[],
        dependencies=[],
        owner_class=None,
        closure_vars={},
        closure_func_refs={},
    )
    store = Store()
    h = store.put(node)
    store.set_ref(f"{module}.{name}", h)
    return store.to_json()


# ===========================================================================
# NoopExecutor
# ===========================================================================


class TestNoopExecutor:
    @pytest.mark.asyncio
    async def test_simple_function(self) -> None:
        executor = NoopExecutor()
        result = await executor.execute(
            "def f(x):\n    return x * 2\n",
            "f", (21,), {},
        )
        assert result == 42

    @pytest.mark.asyncio
    async def test_with_kwargs(self) -> None:
        executor = NoopExecutor()
        result = await executor.execute(
            "def f(x, y=10):\n    return x + y\n",
            "f", (5,), {"y": 3},
        )
        assert result == 8

    @pytest.mark.asyncio
    async def test_async_function(self) -> None:
        executor = NoopExecutor()
        result = await executor.execute(
            "async def af(x):\n    return x + 10\n",
            "af", (5,), {},
        )
        assert result == 15

    @pytest.mark.asyncio
    async def test_class_method(self) -> None:
        source = (
            "class Greeter:\n"
            "    def greet(self, name):\n"
            "        return f'hello {name}'\n"
        )
        executor = NoopExecutor()
        # For a method, we need an instance
        ns: dict = {}
        exec(compile(source, "<test>", "exec"), ns)
        instance = ns["Greeter"]()
        result = await executor.execute(
            source, "greet", (instance, "world"), {},
            owner_class="m.Greeter",
        )
        assert result == "hello world"

    @pytest.mark.asyncio
    async def test_function_not_found(self) -> None:
        executor = NoopExecutor()
        with pytest.raises(RuntimeError, match="not found"):
            await executor.execute("def g(): pass\n", "f", (), {})

    @pytest.mark.asyncio
    async def test_start_stop_are_noop(self) -> None:
        executor = NoopExecutor()
        await executor.start()
        await executor.stop()
        # Should work fine as context manager too
        async with executor:
            pass


# ===========================================================================
# Protocol
# ===========================================================================


class TestProtocol:
    def test_encode_decode_roundtrip(self) -> None:
        obj = {"hello": "world", "n": 42}
        data = encode(obj)
        assert len(data) > HEADER_SIZE
        length = decode_header(data[:HEADER_SIZE])
        payload = json.loads(data[HEADER_SIZE:HEADER_SIZE + length])
        assert payload == obj

    def test_header_size(self) -> None:
        assert HEADER_SIZE == 4

    @pytest.mark.asyncio
    async def test_async_send_recv(self) -> None:
        """Test async send/recv using an in-memory stream pair."""
        # Create a connected pair of streams via a TCP loopback
        server_ready = asyncio.Event()
        received: list[dict] = []

        async def _server(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            msg = await async_recv(reader)
            received.append(msg)
            await async_send(writer, {"echo": msg})
            writer.close()

        server = await asyncio.start_server(_server, "127.0.0.1", 0)
        addr = server.sockets[0].getsockname()

        reader, writer = await asyncio.open_connection(addr[0], addr[1])
        await async_send(writer, {"test": "data"})
        resp = await async_recv(reader)

        writer.close()
        server.close()
        await server.wait_closed()

        assert received == [{"test": "data"}]
        assert resp == {"echo": {"test": "data"}}


# ===========================================================================
# Guest Agent execution logic (tested without a VM)
# ===========================================================================


class TestGuestAgentExecution:
    @pytest.mark.asyncio
    async def test_simple_function(self) -> None:
        resp = await _execute_request({
            "source": "def f(x):\n    return x * 2\n",
            "function_name": "f",
            "args": [21],
            "kwargs": {},
        })
        assert resp["status"] == "ok"
        assert resp["result"] == 42

    @pytest.mark.asyncio
    async def test_with_kwargs(self) -> None:
        resp = await _execute_request({
            "source": "def f(x, y=10):\n    return x + y\n",
            "function_name": "f",
            "args": [5],
            "kwargs": {"y": 3},
        })
        assert resp["status"] == "ok"
        assert resp["result"] == 8

    @pytest.mark.asyncio
    async def test_async_function(self) -> None:
        resp = await _execute_request({
            "source": "async def af(x):\n    return x + 10\n",
            "function_name": "af",
            "args": [5],
            "kwargs": {},
        })
        assert resp["status"] == "ok"
        assert resp["result"] == 15

    @pytest.mark.asyncio
    async def test_multiple_functions(self) -> None:
        source = "def helper(x):\n    return x * 3\n\ndef f(x):\n    return helper(x) + 1\n"
        resp = await _execute_request({
            "source": source,
            "function_name": "f",
            "args": [5],
            "kwargs": {},
        })
        assert resp["status"] == "ok"
        assert resp["result"] == 16

    @pytest.mark.asyncio
    async def test_error_returns_traceback(self) -> None:
        resp = await _execute_request({
            "source": "def f(x):\n    raise ValueError('boom')\n",
            "function_name": "f",
            "args": [1],
            "kwargs": {},
        })
        assert resp["status"] == "error"
        assert resp["error_type"] == "ValueError"
        assert "boom" in resp["error_message"]
        assert resp["error_traceback"] is not None

    @pytest.mark.asyncio
    async def test_function_not_found(self) -> None:
        resp = await _execute_request({
            "source": "def g(): pass\n",
            "function_name": "f",
            "args": [],
            "kwargs": {},
        })
        assert resp["status"] == "error"
        assert "not found" in resp["error_message"]

    @pytest.mark.asyncio
    async def test_syntax_error(self) -> None:
        resp = await _execute_request({
            "source": "def f(x) return x\n",  # invalid syntax
            "function_name": "f",
            "args": [],
            "kwargs": {},
        })
        assert resp["status"] == "error"
        assert resp["error_type"] == "SyntaxError"

    @pytest.mark.asyncio
    async def test_class_method(self) -> None:
        source = (
            "class Greeter:\n"
            "    def greet(self, name):\n"
            "        return f'hello {name}'\n"
        )
        resp = await _execute_request({
            "source": source,
            "function_name": "greet",
            "args": [{"__pyfuse_obj__": {"class": "Greeter", "state": {}}}, "world"],
            "kwargs": {},
            "owner_class": "m.Greeter",
        })
        assert resp["status"] == "ok"
        assert resp["result"] == "hello world"

    @pytest.mark.asyncio
    async def test_object_resolution(self) -> None:
        """Test that __pyfuse_obj__ sentinels in args are resolved."""
        source = (
            "class Point:\n"
            "    pass\n\n"
            "def distance(p):\n"
            "    return (p.x ** 2 + p.y ** 2) ** 0.5\n"
        )
        resp = await _execute_request({
            "source": source,
            "function_name": "distance",
            "args": [{"__pyfuse_obj__": {"class": "Point", "state": {"x": 3, "y": 4}}}],
            "kwargs": {},
        })
        assert resp["status"] == "ok"
        assert resp["result"] == 5.0


# ===========================================================================
# Guest Agent TCP server (integration)
# ===========================================================================


class TestGuestAgentServer:
    @pytest.mark.asyncio
    async def test_end_to_end(self) -> None:
        """Start the guest agent as a TCP server and execute a request."""
        from pyfuse.worker.sandbox.guest_agent import serve as guest_serve

        # Start server on a random port
        server = await asyncio.start_server(
            lambda r, w: None, "127.0.0.1", 0
        )
        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()

        # Start guest agent in background
        from pyfuse.worker.sandbox.guest_agent import _handle_client
        agent_server = await asyncio.start_server(
            _handle_client, "127.0.0.1", port,
        )

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            await async_send(writer, {
                "source": "def f(x):\n    return x ** 2\n",
                "function_name": "f",
                "args": [7],
                "kwargs": {},
            })
            resp = await async_recv(reader)
            assert resp["status"] == "ok"
            assert resp["result"] == 49

            # Test second request on the same connection
            await async_send(writer, {
                "source": "def g(a, b):\n    return a + b\n",
                "function_name": "g",
                "args": [3, 4],
                "kwargs": {},
            })
            resp = await async_recv(reader)
            assert resp["status"] == "ok"
            assert resp["result"] == 7

            writer.close()
        finally:
            agent_server.close()
            await agent_server.wait_closed()


# ===========================================================================
# SandboxConfig & create_executor
# ===========================================================================


class TestSandboxConfig:
    def test_defaults(self) -> None:
        cfg = SandboxConfig()
        assert cfg.enabled is False
        assert cfg.backend == "vm"
        assert cfg.vm_name == "pyfuse-sandbox"
        assert cfg.guest_port == 9749
        assert cfg.cpus == 2
        assert cfg.memory_gb == 2
        assert cfg.timeout == 60.0
        assert cfg.boot_timeout == 30.0
        assert cfg.ssh_key_path is None
        assert cfg.docker_image == "pyfuse-sandbox"
        assert cfg.docker_container_name == "pyfuse-sandbox"

    def test_frozen(self) -> None:
        cfg = SandboxConfig()
        with pytest.raises(AttributeError):
            cfg.enabled = True  # type: ignore[misc]

    def test_docker_backend(self) -> None:
        cfg = SandboxConfig(enabled=True, backend="docker")
        assert cfg.backend == "docker"
        assert cfg.docker_image == "pyfuse-sandbox"


class TestCreateExecutor:
    def test_none_returns_noop(self) -> None:
        executor = create_executor(None)
        assert isinstance(executor, NoopExecutor)

    def test_disabled_returns_noop(self) -> None:
        executor = create_executor(SandboxConfig(enabled=False))
        assert isinstance(executor, NoopExecutor)

    def test_enabled_returns_vm_executor(self) -> None:
        from pyfuse.worker.sandbox.vm import VMExecutor
        executor = create_executor(SandboxConfig(enabled=True))
        assert isinstance(executor, VMExecutor)

    def test_enabled_docker_returns_docker_executor(self) -> None:
        from pyfuse.worker.sandbox.docker import DockerExecutor
        executor = create_executor(SandboxConfig(enabled=True, backend="docker"))
        assert isinstance(executor, DockerExecutor)

    def test_disabled_docker_returns_noop(self) -> None:
        executor = create_executor(SandboxConfig(enabled=False, backend="docker"))
        assert isinstance(executor, NoopExecutor)


# ===========================================================================
# Worker integration with NoopExecutor (default path unchanged)
# ===========================================================================


class TestWorkerWithNoopSandbox:
    @pytest.mark.asyncio
    async def test_default_worker_uses_noop(self) -> None:
        worker = Worker(auto_install=False)
        assert isinstance(worker._sandbox, NoopExecutor)
        assert not worker.sandboxed

    @pytest.mark.asyncio
    async def test_explicit_noop(self) -> None:
        worker = Worker(auto_install=False, sandbox=NoopExecutor())
        assert not worker.sandboxed

    @pytest.mark.asyncio
    async def test_config_disabled(self) -> None:
        worker = Worker(auto_install=False, sandbox=SandboxConfig(enabled=False))
        assert not worker.sandboxed

    @pytest.mark.asyncio
    async def test_config_enabled(self) -> None:
        from pyfuse.worker.sandbox.vm import VMExecutor
        worker = Worker(auto_install=False, sandbox=SandboxConfig(enabled=True))
        assert worker.sandboxed
        assert isinstance(worker._sandbox, VMExecutor)

    @pytest.mark.asyncio
    async def test_execute_still_works_without_sandbox(self) -> None:
        """Ensure the default (no-sandbox) path is not broken."""
        json_str = _make_store_json("def f(x):\n    return x * 2\n")
        task = Task(graph_json=json_str, function_name="f", args=(21,))
        worker = Worker(auto_install=False)
        assert await worker.run(task) == 42

    @pytest.mark.asyncio
    async def test_execute_async_without_sandbox(self) -> None:
        json_str = _make_store_json("async def f(x):\n    return x + 10\n")
        task = Task(graph_json=json_str, function_name="f", args=(5,))
        worker = Worker(auto_install=False)
        assert await worker.run(task) == 15


# ===========================================================================
# Worker integration with a custom SandboxExecutor
# ===========================================================================


class _FakeSandbox(SandboxExecutor):
    """Test double that records calls and returns a fixed value."""

    def __init__(self, return_value: object = 42) -> None:
        self.calls: list[dict] = []
        self._return_value = return_value

    async def execute(
        self,
        source: str,
        function_name: str,
        args: tuple,
        kwargs: dict,
        *,
        owner_class: str | None = None,
    ) -> object:
        self.calls.append({
            "source": source,
            "function_name": function_name,
            "args": args,
            "kwargs": kwargs,
            "owner_class": owner_class,
        })
        return self._return_value


class TestWorkerWithFakeSandbox:
    @pytest.mark.asyncio
    async def test_delegates_to_sandbox(self) -> None:
        sandbox = _FakeSandbox(return_value=99)
        worker = Worker(auto_install=False, sandbox=sandbox)
        assert worker.sandboxed

        json_str = _make_store_json("def f(x):\n    return x * 2\n")
        task = Task(graph_json=json_str, function_name="f", args=(21,))
        result = await worker.run(task)

        assert result == 99
        assert len(sandbox.calls) == 1
        assert sandbox.calls[0]["function_name"] == "f"
        assert sandbox.calls[0]["args"] == (21,)

    @pytest.mark.asyncio
    async def test_caching_still_works(self) -> None:
        sandbox = _FakeSandbox(return_value=0)
        worker = Worker(auto_install=False, sandbox=sandbox)

        json_str = _make_store_json("def f(x):\n    return x\n")
        task1 = Task(graph_json=json_str, function_name="f", args=(1,))
        task2 = Task(graph_json=json_str, function_name="f", args=(2,))

        await worker.run(task1)
        await worker.run(task2)

        # Cache should still work (same graph key)
        assert worker.cache_info()["size"] == 1
        # But sandbox should be called twice (different args)
        assert len(sandbox.calls) == 2


# ===========================================================================
# DockerExecutor unit tests (no Docker daemon needed)
# ===========================================================================


class TestDockerExecutor:
    def test_instantiation_with_config(self) -> None:
        from pyfuse.worker.sandbox.docker import DockerExecutor
        cfg = SandboxConfig(enabled=True, backend="docker", docker_image="my-img")
        executor = DockerExecutor(cfg)
        assert executor._cfg.docker_image == "my-img"
        assert executor._cfg.backend == "docker"
        assert not executor._started

    def test_instantiation_default_config(self) -> None:
        from pyfuse.worker.sandbox.docker import DockerExecutor
        executor = DockerExecutor()
        assert executor._cfg.docker_image == "pyfuse-sandbox"
        assert executor._cfg.backend == "docker"

    def test_is_sandbox_executor(self) -> None:
        from pyfuse.worker.sandbox.docker import DockerExecutor
        executor = DockerExecutor()
        assert isinstance(executor, SandboxExecutor)
        assert not isinstance(executor, NoopExecutor)

    def test_context_manager_protocol(self) -> None:
        """DockerExecutor supports async context manager (from base class)."""
        from pyfuse.worker.sandbox.docker import DockerExecutor
        executor = DockerExecutor()
        assert hasattr(executor, "__aenter__")
        assert hasattr(executor, "__aexit__")


class TestDockerHelpers:
    """Test Docker CLI helper functions."""

    def test_check_docker_available_raises_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pyfuse.worker.sandbox import docker as docker_mod
        monkeypatch.setattr("shutil.which", lambda _cmd: None)
        with pytest.raises(WorkerError, match="docker"):
            docker_mod._check_docker_available()

    def test_dockerfile_dir_contains_dockerfile(self) -> None:
        from pyfuse.worker.sandbox.docker import _DOCKERFILE_DIR
        assert (_DOCKERFILE_DIR / "Dockerfile").exists()

    def test_dockerfile_dir_contains_guest_agent(self) -> None:
        from pyfuse.worker.sandbox.docker import _DOCKERFILE_DIR
        assert (_DOCKERFILE_DIR / "guest_agent.py").exists()


# ===========================================================================
# Worker integration with DockerExecutor (via SandboxConfig)
# ===========================================================================


class TestWorkerWithDockerConfig:
    @pytest.mark.asyncio
    async def test_config_docker_creates_docker_executor(self) -> None:
        from pyfuse.worker.sandbox.docker import DockerExecutor
        worker = Worker(
            auto_install=False,
            sandbox=SandboxConfig(enabled=True, backend="docker"),
        )
        assert worker.sandboxed
        assert isinstance(worker._sandbox, DockerExecutor)

    @pytest.mark.asyncio
    async def test_config_docker_disabled_creates_noop(self) -> None:
        worker = Worker(
            auto_install=False,
            sandbox=SandboxConfig(enabled=False, backend="docker"),
        )
        assert not worker.sandboxed
        assert isinstance(worker._sandbox, NoopExecutor)


# ===========================================================================
# DockerExecutor factory
# ===========================================================================


class TestDockerFactory:
    def test_create_docker_executor_with_config(self) -> None:
        from pyfuse.worker.sandbox.docker import DockerExecutor, create_docker_executor
        cfg = SandboxConfig(enabled=True, backend="docker", docker_image="custom")
        executor = create_docker_executor(cfg)
        assert isinstance(executor, DockerExecutor)
        assert executor._cfg.docker_image == "custom"

    def test_create_docker_executor_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pyfuse.worker.sandbox.docker import DockerExecutor, create_docker_executor
        monkeypatch.delenv("PYFUSE_SANDBOX_DOCKER_IMAGE", raising=False)
        monkeypatch.delenv("PYFUSE_SANDBOX_DOCKER_CONTAINER", raising=False)
        executor = create_docker_executor()
        assert isinstance(executor, DockerExecutor)
        assert executor._cfg.docker_image == "pyfuse-sandbox"
        assert executor._cfg.docker_container_name == "pyfuse-sandbox"

    def test_create_docker_executor_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pyfuse.worker.sandbox.docker import DockerExecutor, create_docker_executor
        monkeypatch.setenv("PYFUSE_SANDBOX_DOCKER_IMAGE", "my-image")
        monkeypatch.setenv("PYFUSE_SANDBOX_DOCKER_CONTAINER", "my-container")
        executor = create_docker_executor()
        assert isinstance(executor, DockerExecutor)
        assert executor._cfg.docker_image == "my-image"
        assert executor._cfg.docker_container_name == "my-container"
