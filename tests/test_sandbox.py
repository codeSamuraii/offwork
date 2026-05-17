"""Tests for the Docker sandbox subsystem.

These tests exercise:
  - DockerSandbox class (instantiation, configuration)
  - Guest agent protocol and execution logic
  - Worker integration (with and without sandbox)

DockerSandbox integration tests that require a running Docker daemon
are skipped unless Docker is available.
"""

import asyncio
import json

import pytest

from offwork.core.errors import WorkerError
from offwork.core.task import Task
from offwork.worker.sandbox import DockerSandbox
from offwork.worker.sandbox._protocol import encode, decode_header, HEADER_SIZE, async_send, async_recv
from offwork.worker.sandbox.guest_agent import _execute_request
from offwork.worker.worker import Worker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_source(body: str) -> str:
    """Wrap a function body into a proper def."""
    return f"def f(x):\n    return {body}\n"


def _make_store_json(source: str, name: str = "f", module: str = "m") -> str:
    """Build a minimal Store JSON string for a single function."""
    from offwork.core.models import FunctionNode, ImportInfo
    from offwork.graph.store import Store

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
# Guest Agent execution logic
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
            "args": [{"__offwork_obj__": {"class": "Greeter", "state": {}}}, "world"],
            "kwargs": {},
            "owner_class": "m.Greeter",
        })
        assert resp["status"] == "ok"
        assert resp["result"] == "hello world"

    @pytest.mark.asyncio
    async def test_object_resolution(self) -> None:
        """Test that __offwork_obj__ sentinels in args are resolved."""
        source = (
            "class Point:\n"
            "    pass\n\n"
            "def distance(p):\n"
            "    return (p.x ** 2 + p.y ** 2) ** 0.5\n"
        )
        resp = await _execute_request({
            "source": source,
            "function_name": "distance",
            "args": [{"__offwork_obj__": {"class": "Point", "state": {"x": 3, "y": 4}}}],
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
        server = await asyncio.start_server(
            lambda r, w: None, "127.0.0.1", 0
        )
        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()

        from offwork.worker.sandbox.guest_agent import _handle_client
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
# DockerSandbox
# ===========================================================================


class TestDockerSandbox:
    def test_default_params(self) -> None:
        sb = DockerSandbox()
        # The default tag embeds a content hash of the bundled image
        # assets so changes to the Dockerfile / guest agent invalidate
        # any previously-built image.
        assert sb.image.startswith("offwork-sandbox:")
        assert sb.container_name == "offwork-sandbox"
        assert sb.guest_port == 9749
        assert sb.cpus == 2
        assert sb.memory_gb == 2
        assert sb.timeout == 60.0
        assert sb.boot_timeout == 30.0
        assert not sb._started

    def test_custom_params(self) -> None:
        sb = DockerSandbox(image="my-img", container_name="my-box", timeout=120.0)
        assert sb.image == "my-img"
        assert sb.container_name == "my-box"
        assert sb.timeout == 120.0

    def test_context_manager_protocol(self) -> None:
        sb = DockerSandbox()
        assert hasattr(sb, "__aenter__")
        assert hasattr(sb, "__aexit__")


class TestDockerHelpers:
    """Test Docker CLI helper functions."""

    def test_check_docker_available_raises_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from offwork.worker.sandbox import docker as docker_mod
        monkeypatch.setattr("shutil.which", lambda _cmd: None)
        with pytest.raises(WorkerError, match="docker"):
            docker_mod._check_docker_available()

    def test_dockerfile_dir_contains_dockerfile(self) -> None:
        from offwork.worker.sandbox.docker import _DOCKERFILE_DIR
        assert (_DOCKERFILE_DIR / "Dockerfile").exists()

    def test_dockerfile_dir_contains_guest_agent(self) -> None:
        from offwork.worker.sandbox.docker import _DOCKERFILE_DIR
        assert (_DOCKERFILE_DIR / "guest_agent.py").exists()


# ===========================================================================
# Worker without sandbox (default path)
# ===========================================================================


class TestWorkerWithoutSandbox:
    @pytest.mark.asyncio
    async def test_default_worker_not_sandboxed(self) -> None:
        worker = Worker(auto_install=False)
        assert worker._sandbox is None
        assert not worker.sandboxed

    @pytest.mark.asyncio
    async def test_execute_sync_function(self) -> None:
        json_str = _make_store_json("def f(x):\n    return x * 2\n")
        task = Task(graph_json=json_str, function_name="f", args=(21,))
        worker = Worker(auto_install=False)
        assert await worker.run(task) == 42

    @pytest.mark.asyncio
    async def test_execute_async_function(self) -> None:
        json_str = _make_store_json("async def f(x):\n    return x + 10\n")
        task = Task(graph_json=json_str, function_name="f", args=(5,))
        worker = Worker(auto_install=False)
        assert await worker.run(task) == 15

    @pytest.mark.asyncio
    async def test_sandbox_false_means_no_sandbox(self) -> None:
        worker = Worker(auto_install=False, sandbox=False)
        assert not worker.sandboxed

    @pytest.mark.asyncio
    async def test_sandbox_none_means_no_sandbox(self) -> None:
        worker = Worker(auto_install=False, sandbox=None)
        assert not worker.sandboxed


# ===========================================================================
# Worker with sandbox enabled
# ===========================================================================


class TestWorkerWithSandbox:
    @pytest.mark.asyncio
    async def test_sandbox_true_creates_docker_sandbox(self) -> None:
        worker = Worker(auto_install=False, sandbox=True)
        assert worker.sandboxed
        assert isinstance(worker._sandbox, DockerSandbox)

    @pytest.mark.asyncio
    async def test_sandbox_instance(self) -> None:
        sb = DockerSandbox(image="custom-img")
        worker = Worker(auto_install=False, sandbox=sb)
        assert worker.sandboxed
        assert worker._sandbox is sb
        assert worker._sandbox.image == "custom-img"


# ===========================================================================
# Worker with a fake sandbox (duck-typed)
# ===========================================================================


class _FakeSandbox:
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
        worker = Worker(auto_install=False)
        worker._sandbox = sandbox  # type: ignore[assignment]

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
        worker = Worker(auto_install=False)
        worker._sandbox = sandbox  # type: ignore[assignment]

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
# Guest agent progress support
# ===========================================================================


class TestGuestAgentProgress:
    """Test that offwork.progress() calls inside the guest agent produce
    ``{"status": "progress", ...}`` frames on the wire."""

    @pytest.mark.asyncio
    async def test_progress_from_sync_function(self) -> None:
        """Sync function calling offwork.progress() sends progress frames."""
        source = (
            "from offwork import progress\n\n"
            "def f(n):\n"
            "    for i in range(n):\n"
            "        progress(i + 1, n, message=f'step {i+1}')\n"
            "    return 'done'\n"
        )

        # Start the agent on a random port
        from offwork.worker.sandbox.guest_agent import _handle_client
        agent_server = await asyncio.start_server(
            _handle_client, "127.0.0.1", 0,
        )
        port = agent_server.sockets[0].getsockname()[1]

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            await async_send(writer, {
                "source": source,
                "function_name": "f",
                "args": [3],
                "kwargs": {},
            })

            # Expect 3 progress messages then the final ok
            messages = []
            while True:
                msg = await async_recv(reader)
                messages.append(msg)
                if msg["status"] != "progress":
                    break

            writer.close()
        finally:
            agent_server.close()
            await agent_server.wait_closed()

        assert len(messages) == 4  # 3 progress + 1 ok
        for i, m in enumerate(messages[:3]):
            assert m["status"] == "progress"
            assert m["current"] == i + 1
            assert m["total"] == 3
            assert m["message"] == f"step {i+1}"
        assert messages[-1] == {"status": "ok", "result": "done"}

    @pytest.mark.asyncio
    async def test_progress_from_async_function(self) -> None:
        """Async function calling offwork.progress() sends progress frames."""
        source = (
            "import offwork\n\n"
            "async def af(n):\n"
            "    for i in range(n):\n"
            "        offwork.progress(i + 1, n)\n"
            "    return n\n"
        )

        from offwork.worker.sandbox.guest_agent import _handle_client
        agent_server = await asyncio.start_server(
            _handle_client, "127.0.0.1", 0,
        )
        port = agent_server.sockets[0].getsockname()[1]

        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            await async_send(writer, {
                "source": source,
                "function_name": "af",
                "args": [2],
                "kwargs": {},
            })

            messages = []
            while True:
                msg = await async_recv(reader)
                messages.append(msg)
                if msg["status"] != "progress":
                    break

            writer.close()
        finally:
            agent_server.close()
            await agent_server.wait_closed()

        assert len(messages) == 3  # 2 progress + 1 ok
        assert messages[0] == {"status": "progress", "current": 1, "total": 2}
        assert messages[1] == {"status": "progress", "current": 2, "total": 2}
        assert messages[2] == {"status": "ok", "result": 2}

    @pytest.mark.asyncio
    async def test_no_progress_still_works(self) -> None:
        """Functions that don't call progress() work as before."""
        resp = await _execute_request({
            "source": "def f(x):\n    return x + 1\n",
            "function_name": "f",
            "args": [5],
            "kwargs": {},
        })
        assert resp == {"status": "ok", "result": 6}

    @pytest.mark.asyncio
    async def test_offwork_shim_cleaned_up(self) -> None:
        """The fake offwork module is removed after execution."""
        import sys
        had_offwork = "offwork" in sys.modules
        original = sys.modules.get("offwork")

        await _execute_request({
            "source": "from offwork import progress\ndef f():\n    return 1\n",
            "function_name": "f",
            "args": [],
            "kwargs": {},
        })

        if had_offwork:
            assert sys.modules.get("offwork") is original
        # offwork IS in sys.modules because the test suite imports it,
        # so just verify it's the real one, not a fake
        assert hasattr(sys.modules["offwork"], "trace")


# ===========================================================================
# DockerSandbox progress forwarding
# ===========================================================================


class TestDockerSandboxProgressForwarding:
    """Test that DockerSandbox._read_response forwards progress messages."""

    @pytest.mark.asyncio
    async def test_read_response_forwards_progress(self) -> None:
        """_read_response calls progress_cb for each progress frame."""
        from offwork.worker.sandbox.docker import DockerSandbox
        from offwork.worker.sandbox._protocol import encode

        progress_calls: list[tuple] = []

        def _on_progress(current: float, total: float | None, message: str | None) -> None:
            progress_calls.append((current, total, message))

        # Set up a fake server that sends 2 progress frames + final ok
        async def _fake_agent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            _req = await async_recv(reader)
            await async_send(writer, {"status": "progress", "current": 1, "total": 3})
            await async_send(writer, {"status": "progress", "current": 2, "total": 3, "message": "half"})
            await async_send(writer, {"status": "ok", "result": 42})
            writer.close()

        server = await asyncio.start_server(_fake_agent, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        try:
            sb = DockerSandbox()
            sb._reader, sb._writer = await asyncio.open_connection("127.0.0.1", port)
            sb._started = True
            sb._host_port = port

            resp = await sb._send_request(
                {"source": "...", "function_name": "f", "args": [], "kwargs": {}},
                progress_cb=_on_progress,
            )
        finally:
            server.close()
            await server.wait_closed()

        assert resp == {"status": "ok", "result": 42}
        assert len(progress_calls) == 2
        assert progress_calls[0] == (1, 3, None)
        assert progress_calls[1] == (2, 3, "half")