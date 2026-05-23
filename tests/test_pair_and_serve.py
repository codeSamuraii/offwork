"""Integration tests for the pairing flow and the signed envelope.

Verifies:
- ``offwork pair`` produces matching keys on both sides.
- A paired client + token-paired worker can submit signed envelopes
  end-to-end through a local backend.
- Unsigned envelopes are rejected by a worker that requires signing.
"""

import asyncio
import socket
from pathlib import Path

import pytest

import offwork
from offwork.core import ed25519
from offwork.core.pairing import (
    PairingResult,
    generate_pin,
    initiate_pairing,
    load_shared_key,
    respond_to_pairing,
    save_shared_key,
)
from offwork.core.signing import NonceLRU
from offwork.core.clients import KnownClients
from offwork.core.envelope import build_signed_envelope
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


async def _pair_pair() -> tuple[PairingResult, PairingResult]:
    backend = _MockPairingBackend()
    pin = generate_pin()

    async def worker_side() -> PairingResult:
        return await initiate_pairing(backend, pin, timeout=5.0)

    async def client_side() -> PairingResult:
        await asyncio.sleep(0.1)
        return await respond_to_pairing(backend, pin, timeout=5.0)

    return await asyncio.gather(worker_side(), client_side())  # type: ignore[return-value]


class TestPairing:
    @pytest.mark.asyncio
    async def test_pairing_produces_matching_keys(self) -> None:
        worker_result, client_result = await _pair_pair()
        assert worker_result.shared_key == client_result.shared_key
        assert len(worker_result.shared_key) == 32

    @pytest.mark.asyncio
    async def test_key_persistence(self, tmp_path: Path) -> None:
        worker_result, client_result = await _pair_pair()
        save_shared_key(worker_result.shared_key, "worker", key_dir=tmp_path)
        save_shared_key(client_result.shared_key, "client", key_dir=tmp_path)
        worker_key = load_shared_key("worker", key_dir=tmp_path)
        client_key = load_shared_key("client", key_dir=tmp_path)
        assert worker_key == client_key == worker_result.shared_key


class TestSignedEnvelopeRoundtrip:
    @pytest.mark.asyncio
    async def test_signed_task_executes(self) -> None:
        worker_result, client_result = await _pair_pair()
        root_token = client_result.shared_key

        @offwork.task
        def add(a: int, b: int) -> int:
            return a + b

        graph = Graph.default()
        store = graph.to_store(add)
        graph_json = store.to_json()
        func_qname = next(qn for qn in store.refs if qn.endswith(".add"))

        client_id = "ab" * 16
        seed = ed25519.generate_seed()
        pub = ed25519.seed_to_public(seed)

        task = Task(graph_json=graph_json, function_name=func_qname, args=(3, 4))
        envelope = build_signed_envelope(
            task,
            root_token=root_token,
            client_id=client_id,
            identity_seed=seed,
            public_key=pub,
        )

        port = _free_port()
        transport = LocalBackend(f"local://127.0.0.1:{port}", server=True)
        try:
            await transport.submit(envelope)
            worker = Worker(auto_install=False)
            known = KnownClients(key_dir=Path("/tmp") / f"offwork-test-{port}")
            async for task_json in transport.listen():
                await _handle_task(
                    worker, transport, task_json,
                    root_token=root_token,
                    known_clients=known,
                    nonce_lru=NonceLRU(),
                )
                break
            raw = await transport.get_result(task.task_id, timeout=5.0)
            envelope_result = ResultEnvelope.from_json(raw)
            assert envelope_result.status == "ok"
            assert envelope_result.result == 7
            # Worker should have pinned the client id.
            assert known.get(client_id) is not None
        finally:
            await transport.close()

    @pytest.mark.asyncio
    async def test_unsigned_task_rejected(self) -> None:
        worker_result, _ = await _pair_pair()
        root_token = worker_result.shared_key

        @offwork.task
        def noop() -> None:
            pass

        graph = Graph.default()
        store = graph.to_store(noop)
        graph_json = store.to_json()
        func_qname = next(qn for qn in store.refs if qn.endswith(".noop"))

        port = _free_port()
        transport = LocalBackend(f"local://127.0.0.1:{port}", server=True)
        try:
            task = Task(graph_json=graph_json, function_name=func_qname)
            await transport.submit(task.to_json())  # unsigned

            worker = Worker(auto_install=False)
            known = KnownClients(key_dir=Path("/tmp") / f"offwork-test-{port}-u")
            async for task_json in transport.listen():
                await _handle_task(
                    worker, transport, task_json,
                    root_token=root_token,
                    known_clients=known,
                    nonce_lru=NonceLRU(),
                )
                break
            raw = await transport.get_result(task.task_id, timeout=5.0)
            envelope_result = ResultEnvelope.from_json(raw)
            assert envelope_result.status == "error"
            assert envelope_result.error_type == "SignatureError"
        finally:
            await transport.close()
