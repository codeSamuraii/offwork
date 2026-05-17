"""Integration tests for the simplified pairing flow.

Verifies:
- ``offwork worker --pair`` generates a PIN, pairs, and starts serving with signing
- ``offwork pair --backend ...`` pairs as a client and can submit signed tasks
- End-to-end: pair → submit signed task → receive result
"""

import asyncio
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from offwork import trace
from offwork.core.pairing import (
    PairingResult,
    generate_pin,
    initiate_pairing,
    load_shared_key,
    respond_to_pairing,
    save_shared_key,
)
from offwork.core.signing import derive_key
from offwork.core.task import Task
from offwork.graph.graph import Graph
from offwork.worker.backends.local import LocalBackend
from offwork.worker.remote import _handle_task
from offwork.worker.result import ResultEnvelope
from offwork.worker.worker import Worker


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _MockPairingBackend:
    """Minimal in-memory backend supporting pairing protocol operations."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def send_progress(self, key: str, value: str) -> None:
        self._store[key] = value

    async def get_progress(self, key: str) -> str | None:
        return self._store.get(key)


class TestPairThenServe:
    """Test the worker --pair flow: pair first, then serve with signing."""

    @pytest.mark.asyncio
    async def test_pairing_produces_matching_keys(self) -> None:
        """Worker and client derive the same shared key via pairing."""
        backend = _MockPairingBackend()
        pin = generate_pin()

        async def worker_side() -> PairingResult:
            return await initiate_pairing(backend, pin, timeout=5.0)

        async def client_side() -> PairingResult:
            await asyncio.sleep(0.1)
            return await respond_to_pairing(backend, pin, timeout=5.0)

        worker_result, client_result = await asyncio.gather(
            worker_side(), client_side()
        )

        assert worker_result.shared_key == client_result.shared_key
        assert len(worker_result.shared_key) == 32

    @pytest.mark.asyncio
    async def test_key_persistence(self, tmp_path: Path) -> None:
        """Pairing keys can be saved and loaded for signing."""
        backend = _MockPairingBackend()
        pin = "999999"

        async def worker_side() -> PairingResult:
            return await initiate_pairing(backend, pin, timeout=5.0)

        async def client_side() -> PairingResult:
            await asyncio.sleep(0.1)
            return await respond_to_pairing(backend, pin, timeout=5.0)

        worker_result, client_result = await asyncio.gather(
            worker_side(), client_side()
        )

        # Save keys
        save_shared_key(worker_result.shared_key, "worker", key_dir=tmp_path)
        save_shared_key(client_result.shared_key, "client", key_dir=tmp_path)

        # Load and verify they match
        worker_key = load_shared_key("worker", key_dir=tmp_path)
        client_key = load_shared_key("client", key_dir=tmp_path)
        assert worker_key is not None
        assert client_key is not None
        assert worker_key == client_key

    @pytest.mark.asyncio
    async def test_signed_task_roundtrip(self) -> None:
        """A paired client can submit a signed task that a paired worker accepts."""
        # Simulate pairing
        backend = _MockPairingBackend()
        pin = "123456"

        async def worker_side() -> PairingResult:
            return await initiate_pairing(backend, pin, timeout=5.0)

        async def client_side() -> PairingResult:
            await asyncio.sleep(0.1)
            return await respond_to_pairing(backend, pin, timeout=5.0)

        worker_result, client_result = await asyncio.gather(
            worker_side(), client_side()
        )

        # Derive signing keys (same as what serve() and submit_remote() do)
        client_signing_key = derive_key(client_result.shared_key)
        worker_signing_key = derive_key(worker_result.shared_key)

        # Create and sign a task
        @trace
        def add(a: int, b: int) -> int:
            return a + b

        graph = Graph.default()
        store = graph.to_store(add)
        graph_json = store.to_json()

        # Find the correct qualified name from the store refs
        func_qname = next(
            qn for qn in store.refs
            if qn.endswith(".add")
        )

        task = Task(
            graph_json=graph_json,
            function_name=func_qname,
            args=(3, 4),
            kwargs={},
        )
        signed_json = task.to_json(signing_key=client_signing_key)

        # Worker side: verify and execute
        restored = Task.from_json(signed_json, signing_key=worker_signing_key)
        assert restored.function_name == task.function_name
        assert restored.args == (3, 4)

        worker = Worker(auto_install=False)
        result = await worker.run(restored)
        assert result == 7

    @pytest.mark.asyncio
    async def test_signed_task_execution_via_backend(self) -> None:
        """End-to-end: pair → sign → submit → worker processes → result."""
        # Simulate pairing to get shared keys
        pairing_backend = _MockPairingBackend()
        pin = "654321"

        async def worker_side() -> PairingResult:
            return await initiate_pairing(pairing_backend, pin, timeout=5.0)

        async def client_side() -> PairingResult:
            await asyncio.sleep(0.1)
            return await respond_to_pairing(pairing_backend, pin, timeout=5.0)

        worker_result, client_result = await asyncio.gather(
            worker_side(), client_side()
        )

        client_signing_key = derive_key(client_result.shared_key)
        worker_signing_key = derive_key(worker_result.shared_key)

        # Use local backend for task transport
        port = _free_port()
        transport = LocalBackend(f"local://127.0.0.1:{port}", server=True)

        try:
            @trace
            def multiply(x: int, y: int) -> int:
                return x * y

            graph = Graph.default()
            store = graph.to_store(multiply)
            graph_json = store.to_json()

            # Find the correct qualified name from the store refs
            func_qname = next(
                qn for qn in store.refs
                if qn.endswith(".multiply")
            )

            task = Task(
                graph_json=graph_json,
                function_name=func_qname,
                args=(6, 7),
                kwargs={},
            )
            signed_json = task.to_json(signing_key=client_signing_key)

            # Submit the signed task
            await transport.submit(signed_json)

            # Worker processes the task with signing verification
            worker = Worker(auto_install=False)

            async for task_json in transport.listen():
                await _handle_task(
                    worker, transport, task_json,
                    signing_key=worker_signing_key,
                )
                break

            # Fetch the result
            raw = await transport.get_result(task.task_id, timeout=5.0)
            envelope = ResultEnvelope.from_json(raw)
            assert envelope.status == "ok"
            assert envelope.result == 42
        finally:
            await transport.close()

    @pytest.mark.asyncio
    async def test_unsigned_task_rejected_by_paired_worker(self) -> None:
        """A worker with signing enabled rejects unsigned tasks."""
        # Get a signing key via pairing
        pairing_backend = _MockPairingBackend()
        pin = "111111"

        async def worker_side() -> PairingResult:
            return await initiate_pairing(pairing_backend, pin, timeout=5.0)

        async def client_side() -> PairingResult:
            await asyncio.sleep(0.1)
            return await respond_to_pairing(pairing_backend, pin, timeout=5.0)

        worker_result, _ = await asyncio.gather(worker_side(), client_side())
        worker_signing_key = derive_key(worker_result.shared_key)

        port = _free_port()
        transport = LocalBackend(f"local://127.0.0.1:{port}", server=True)

        try:
            @trace
            def noop() -> None:
                pass

            graph = Graph.default()
            store = graph.to_store(noop)
            graph_json = store.to_json()

            func_qname = next(
                qn for qn in store.refs
                if qn.endswith(".noop")
            )

            # Submit an UNSIGNED task
            task = Task(
                graph_json=graph_json,
                function_name=func_qname,
                args=(),
                kwargs={},
            )
            unsigned_json = task.to_json()  # no signing key
            await transport.submit(unsigned_json)

            worker = Worker(auto_install=False)

            async for task_json in transport.listen():
                await _handle_task(
                    worker, transport, task_json,
                    signing_key=worker_signing_key,
                )
                break

            # Worker should have sent an error result
            raw = await transport.get_result(task.task_id, timeout=5.0)
            envelope = ResultEnvelope.from_json(raw)
            assert envelope.status == "error"
            assert envelope.error_type == "SignatureError"
        finally:
            await transport.close()
