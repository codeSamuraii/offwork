"""Tests for pyfuse.core.signing — Ed25519 key pairs and trust stores."""

import dataclasses
import os
import tempfile
from pathlib import Path

import pytest

from pyfuse.core.signing import KeyPair, TrustStore, _fingerprint, _verify
from pyfuse.core.task import Task
from pyfuse.core.errors import TrustError
from pyfuse.worker.worker import Worker


# ===========================================================================
# KeyPair
# ===========================================================================


class TestKeyPairGeneration:
    def test_generate_creates_unique_keys(self) -> None:
        kp1 = KeyPair.generate()
        kp2 = KeyPair.generate()
        assert kp1.fingerprint != kp2.fingerprint

    def test_public_bytes_length(self) -> None:
        kp = KeyPair.generate()
        assert len(kp.public_bytes) == 32

    def test_fingerprint_is_hex_sha256(self) -> None:
        kp = KeyPair.generate()
        assert len(kp.fingerprint) == 64  # SHA-256 hex
        int(kp.fingerprint, 16)  # valid hex

    def test_sign_returns_64_bytes(self) -> None:
        kp = KeyPair.generate()
        sig = kp.sign(b"test data")
        assert len(sig) == 64

    def test_sign_is_deterministic(self) -> None:
        """Ed25519 signatures are deterministic for the same key+message."""
        kp = KeyPair.generate()
        sig1 = kp.sign(b"hello")
        sig2 = kp.sign(b"hello")
        assert sig1 == sig2

    def test_different_data_different_signature(self) -> None:
        kp = KeyPair.generate()
        sig1 = kp.sign(b"hello")
        sig2 = kp.sign(b"world")
        assert sig1 != sig2


class TestKeyPairPersistence:
    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        kp = KeyPair.generate()
        priv_path = tmp_path / "key.pem"
        kp.save(priv_path)
        loaded = KeyPair.from_file(priv_path)
        assert loaded.fingerprint == kp.fingerprint

    def test_private_key_permissions(self, tmp_path: Path) -> None:
        kp = KeyPair.generate()
        priv_path = tmp_path / "key.pem"
        kp.save(priv_path)
        mode = os.stat(priv_path).st_mode & 0o777
        assert mode == 0o600

    def test_save_public_key(self, tmp_path: Path) -> None:
        kp = KeyPair.generate()
        pub_path = tmp_path / "key.pub"
        kp.save_public(pub_path)
        assert pub_path.exists()
        content = pub_path.read_text()
        assert "PUBLIC KEY" in content

    def test_from_private_bytes(self) -> None:
        kp = KeyPair.generate()
        # Get the raw seed via the cryptography API
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
        )

        raw = kp._private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        kp2 = KeyPair.from_private_bytes(raw)
        assert kp2.fingerprint == kp.fingerprint

    def test_from_file_wrong_key_type(self, tmp_path: Path) -> None:
        """Loading an RSA key should fail with a clear error."""
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
        )

        rsa_key = rsa.generate_private_key(65537, 2048)
        pem = rsa_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
        path = tmp_path / "rsa.pem"
        path.write_bytes(pem)
        with pytest.raises(TypeError, match="Ed25519"):
            KeyPair.from_file(path)


# ===========================================================================
# _verify / _fingerprint helpers
# ===========================================================================


class TestVerifyHelpers:
    def test_verify_valid_signature(self) -> None:
        kp = KeyPair.generate()
        data = b"test payload"
        sig = kp.sign(data)
        assert _verify(kp.public_bytes, data, sig) is True

    def test_verify_invalid_signature(self) -> None:
        kp = KeyPair.generate()
        data = b"test payload"
        sig = kp.sign(data)
        assert _verify(kp.public_bytes, b"wrong data", sig) is False

    def test_verify_wrong_key(self) -> None:
        kp1 = KeyPair.generate()
        kp2 = KeyPair.generate()
        sig = kp1.sign(b"data")
        assert _verify(kp2.public_bytes, b"data", sig) is False

    def test_verify_corrupted_signature(self) -> None:
        kp = KeyPair.generate()
        sig = kp.sign(b"data")
        bad_sig = bytes([b ^ 0xFF for b in sig])
        assert _verify(kp.public_bytes, b"data", bad_sig) is False

    def test_fingerprint_deterministic(self) -> None:
        raw = b"\x00" * 32
        assert _fingerprint(raw) == _fingerprint(raw)

    def test_fingerprint_different_keys(self) -> None:
        assert _fingerprint(b"\x00" * 32) != _fingerprint(b"\x01" * 32)


# ===========================================================================
# TrustStore
# ===========================================================================


class TestTrustStore:
    def test_empty_store(self) -> None:
        ts = TrustStore()
        assert len(ts) == 0
        assert not ts
        assert ts.fingerprints == frozenset()

    def test_add_public_bytes(self) -> None:
        kp = KeyPair.generate()
        ts = TrustStore()
        fp = ts.add_public_bytes(kp.public_bytes)
        assert fp == kp.fingerprint
        assert ts.is_trusted(fp)
        assert len(ts) == 1
        assert ts

    def test_not_trusted(self) -> None:
        ts = TrustStore()
        assert not ts.is_trusted("abcd" * 16)

    def test_verify_trusted_signer(self) -> None:
        kp = KeyPair.generate()
        ts = TrustStore()
        ts.add_public_bytes(kp.public_bytes)
        data = b"payload"
        sig = kp.sign(data)
        assert ts.verify(data, sig, kp.public_bytes) is True

    def test_verify_untrusted_signer(self) -> None:
        kp1 = KeyPair.generate()
        kp2 = KeyPair.generate()
        ts = TrustStore()
        ts.add_public_bytes(kp1.public_bytes)  # only trust kp1
        sig = kp2.sign(b"data")
        assert ts.verify(b"data", sig, kp2.public_bytes) is False

    def test_verify_bad_signature(self) -> None:
        kp = KeyPair.generate()
        ts = TrustStore()
        ts.add_public_bytes(kp.public_bytes)
        sig = kp.sign(b"original")
        assert ts.verify(b"tampered", sig, kp.public_bytes) is False

    def test_from_fingerprints(self) -> None:
        fp = "a" * 64
        ts = TrustStore.from_fingerprints({fp})
        assert ts.is_trusted(fp)
        assert len(ts) == 1

    def test_fingerprints_property(self) -> None:
        kp1 = KeyPair.generate()
        kp2 = KeyPair.generate()
        ts = TrustStore()
        ts.add_public_bytes(kp1.public_bytes)
        ts.add_public_bytes(kp2.public_bytes)
        assert ts.fingerprints == frozenset({kp1.fingerprint, kp2.fingerprint})


class TestTrustStoreFromDirectory:
    def test_load_pub_files(self, tmp_path: Path) -> None:
        keys_dir = tmp_path / "keys"
        keys_dir.mkdir()
        kp1 = KeyPair.generate()
        kp2 = KeyPair.generate()
        kp1.save_public(keys_dir / "client1.pub")
        kp2.save_public(keys_dir / "client2.pub")

        ts = TrustStore.from_directory(keys_dir)
        assert len(ts) == 2
        assert ts.is_trusted(kp1.fingerprint)
        assert ts.is_trusted(kp2.fingerprint)

    def test_ignores_non_pub_files(self, tmp_path: Path) -> None:
        keys_dir = tmp_path / "keys"
        keys_dir.mkdir()
        kp = KeyPair.generate()
        kp.save_public(keys_dir / "client.pub")
        (keys_dir / "notes.txt").write_text("ignore me")
        (keys_dir / "key.pem").write_text("not a pub")

        ts = TrustStore.from_directory(keys_dir)
        assert len(ts) == 1

    def test_missing_directory_raises(self) -> None:
        with pytest.raises(NotADirectoryError):
            TrustStore.from_directory("/nonexistent/path")

    def test_empty_directory(self, tmp_path: Path) -> None:
        keys_dir = tmp_path / "empty"
        keys_dir.mkdir()
        ts = TrustStore.from_directory(keys_dir)
        assert len(ts) == 0

    def test_add_public_key_file(self, tmp_path: Path) -> None:
        kp = KeyPair.generate()
        pub_path = tmp_path / "client.pub"
        kp.save_public(pub_path)
        ts = TrustStore()
        fp = ts.add_public_key_file(pub_path)
        assert fp == kp.fingerprint


# ===========================================================================
# Task signing / verification
# ===========================================================================


class TestTaskSigning:
    def test_unsigned_task(self) -> None:
        task = Task(graph_json="{}", function_name="f")
        assert not task.is_signed
        assert task.signature is None
        assert task.signer is None
        assert task.signer_fingerprint is None

    def test_sign_returns_new_task(self) -> None:
        kp = KeyPair.generate()
        task = Task(graph_json="{}", function_name="f")
        signed = task.sign(kp)
        assert not task.is_signed  # original unchanged
        assert signed.is_signed
        assert signed.signature is not None
        assert signed.signer is not None

    def test_sign_preserves_fields(self) -> None:
        kp = KeyPair.generate()
        task = Task(
            graph_json='{"data": 1}',
            function_name="m.func",
            args=(1, 2, 3),
            kwargs={"key": "val"},
            task_id="test123",
            timeout=5.0,
            retries=3,
            retry_delay=2.0,
        )
        signed = task.sign(kp)
        assert signed.graph_json == task.graph_json
        assert signed.function_name == task.function_name
        assert signed.args == task.args
        assert signed.kwargs == task.kwargs
        assert signed.task_id == task.task_id
        assert signed.timeout == task.timeout
        assert signed.retries == task.retries
        assert signed.retry_delay == task.retry_delay

    def test_signer_fingerprint(self) -> None:
        kp = KeyPair.generate()
        task = Task(graph_json="{}", function_name="f").sign(kp)
        assert task.signer_fingerprint == kp.fingerprint

    def test_verify_valid(self) -> None:
        kp = KeyPair.generate()
        task = Task(graph_json="{}", function_name="f").sign(kp)
        assert task.verify() is True

    def test_verify_with_trust_store(self) -> None:
        kp = KeyPair.generate()
        ts = TrustStore()
        ts.add_public_bytes(kp.public_bytes)
        task = Task(graph_json="{}", function_name="f").sign(kp)
        assert task.verify(ts) is True

    def test_verify_untrusted_key(self) -> None:
        kp = KeyPair.generate()
        ts = TrustStore()  # empty — no one is trusted
        task = Task(graph_json="{}", function_name="f").sign(kp)
        assert task.verify(ts) is False

    def test_verify_unsigned(self) -> None:
        task = Task(graph_json="{}", function_name="f")
        assert task.verify() is False

    def test_tampered_graph_detected(self) -> None:
        kp = KeyPair.generate()
        task = Task(graph_json='{"ok": true}', function_name="f").sign(kp)
        tampered = dataclasses.replace(task, graph_json='{"evil": true}')
        assert tampered.verify() is False

    def test_tampered_function_name_detected(self) -> None:
        kp = KeyPair.generate()
        task = Task(graph_json="{}", function_name="safe").sign(kp)
        tampered = dataclasses.replace(task, function_name="evil")
        assert tampered.verify() is False

    def test_tampered_args_detected(self) -> None:
        kp = KeyPair.generate()
        task = Task(graph_json="{}", function_name="f", args=(1,)).sign(kp)
        tampered = dataclasses.replace(task, args=(999,))
        assert tampered.verify() is False

    def test_tampered_kwargs_detected(self) -> None:
        kp = KeyPair.generate()
        task = Task(graph_json="{}", function_name="f", kwargs={"a": 1}).sign(kp)
        tampered = dataclasses.replace(task, kwargs={"a": 999})
        assert tampered.verify() is False

    def test_tampered_task_id_detected(self) -> None:
        kp = KeyPair.generate()
        task = Task(graph_json="{}", function_name="f").sign(kp)
        tampered = dataclasses.replace(task, task_id="forged_id")
        assert tampered.verify() is False


class TestTaskSigningRoundtrip:
    """Ensure signing survives JSON serialization / deserialization."""

    def test_signed_task_roundtrip(self) -> None:
        kp = KeyPair.generate()
        task = Task(
            graph_json='{"objects": {}}',
            function_name="m.func",
            args=(1, "two"),
            kwargs={"k": "v"},
            task_id="rt123",
        ).sign(kp)

        json_str = task.to_json()
        restored = Task.from_json(json_str)
        assert restored.is_signed
        assert restored.signature == task.signature
        assert restored.signer == task.signer
        assert restored.verify() is True

    def test_unsigned_task_roundtrip_still_works(self) -> None:
        """Unsigned tasks serialize/deserialize without signature fields."""
        task = Task(graph_json="{}", function_name="f", task_id="test")
        json_str = task.to_json()
        restored = Task.from_json(json_str)
        assert not restored.is_signed
        assert restored.signature is None
        assert restored.signer is None

    def test_signature_fields_in_json(self) -> None:
        import json

        kp = KeyPair.generate()
        task = Task(graph_json="{}", function_name="f").sign(kp)
        data = json.loads(task.to_json())
        assert "signature" in data
        assert "signer" in data

    def test_no_signature_fields_when_unsigned(self) -> None:
        import json

        task = Task(graph_json="{}", function_name="f")
        data = json.loads(task.to_json())
        assert "signature" not in data
        assert "signer" not in data


# ===========================================================================
# Worker trust verification
# ===========================================================================


class TestWorkerTrust:
    """Test that Worker enforces trust when a TrustStore is configured."""

    def _make_store_and_json(self) -> str:
        """Create a minimal store and return its JSON."""
        from pyfuse.core.models import FunctionNode
        from pyfuse.graph.store import Store

        node = FunctionNode(
            qualified_name="m.f",
            name="f",
            module="m",
            source="def f(x):\n    return x * 2\n",
            imports=[],
            dependencies=[],
            closure_vars={},
            closure_func_refs={},
        )
        store = Store()
        h = store.put(node)
        store.set_ref("m.f", h)
        return store.to_json()

    @pytest.mark.asyncio
    async def test_no_trust_store_allows_all(self) -> None:
        """Without a trust store, any task runs (backward compatible)."""
        json_str = self._make_store_and_json()
        task = Task(graph_json=json_str, function_name="f", args=(21,))
        worker = Worker(auto_install=False)
        assert await worker.run(task) == 42

    @pytest.mark.asyncio
    async def test_signed_trusted_task_passes(self) -> None:
        kp = KeyPair.generate()
        ts = TrustStore()
        ts.add_public_bytes(kp.public_bytes)

        json_str = self._make_store_and_json()
        task = Task(graph_json=json_str, function_name="f", args=(21,)).sign(kp)
        worker = Worker(auto_install=False, trust_store=ts)
        assert await worker.run(task) == 42

    @pytest.mark.asyncio
    async def test_unsigned_task_rejected(self) -> None:
        kp = KeyPair.generate()
        ts = TrustStore()
        ts.add_public_bytes(kp.public_bytes)

        json_str = self._make_store_and_json()
        task = Task(graph_json=json_str, function_name="f", args=(21,))
        worker = Worker(auto_install=False, trust_store=ts)
        with pytest.raises(TrustError, match="unsigned"):
            await worker.run(task)

    @pytest.mark.asyncio
    async def test_untrusted_signer_rejected(self) -> None:
        kp_unknown = KeyPair.generate()
        kp_trusted = KeyPair.generate()
        ts = TrustStore()
        ts.add_public_bytes(kp_trusted.public_bytes)  # only trust kp_trusted

        json_str = self._make_store_and_json()
        task = Task(graph_json=json_str, function_name="f", args=(21,)).sign(kp_unknown)
        worker = Worker(auto_install=False, trust_store=ts)
        with pytest.raises(TrustError, match="untrusted"):
            await worker.run(task)

    @pytest.mark.asyncio
    async def test_tampered_task_rejected(self) -> None:
        kp = KeyPair.generate()
        ts = TrustStore()
        ts.add_public_bytes(kp.public_bytes)

        json_str = self._make_store_and_json()
        task = Task(graph_json=json_str, function_name="f", args=(21,)).sign(kp)
        # Tamper with the args after signing
        tampered = dataclasses.replace(task, args=(999,))
        worker = Worker(auto_install=False, trust_store=ts)
        with pytest.raises(TrustError, match="invalid signature"):
            await worker.run(tampered)

    @pytest.mark.asyncio
    async def test_multiple_trusted_clients(self) -> None:
        kp1 = KeyPair.generate()
        kp2 = KeyPair.generate()
        ts = TrustStore()
        ts.add_public_bytes(kp1.public_bytes)
        ts.add_public_bytes(kp2.public_bytes)

        json_str = self._make_store_and_json()
        worker = Worker(auto_install=False, trust_store=ts)

        task1 = Task(graph_json=json_str, function_name="f", args=(5,)).sign(kp1)
        assert await worker.run(task1) == 10

        task2 = Task(graph_json=json_str, function_name="f", args=(7,)).sign(kp2)
        assert await worker.run(task2) == 14

    @pytest.mark.asyncio
    async def test_trust_with_run_with_policy(self) -> None:
        """Trust is checked in run_with_policy too (delegates to run)."""
        kp = KeyPair.generate()
        ts = TrustStore()
        ts.add_public_bytes(kp.public_bytes)

        json_str = self._make_store_and_json()
        task = Task(graph_json=json_str, function_name="f", args=(3,)).sign(kp)
        worker = Worker(auto_install=False, trust_store=ts)
        assert await worker.run_with_policy(task) == 6


# ===========================================================================
# CLI keypair command
# ===========================================================================


class TestCLIKeypair:
    def test_generate_creates_files(self, tmp_path: Path) -> None:
        from pyfuse.__main__ import _cmd_keypair
        import argparse

        out = tmp_path / "mykey.pem"
        args = argparse.Namespace(keypair_action="generate", output=str(out))
        _cmd_keypair(args)

        assert out.exists()
        assert out.with_suffix(".pub").exists()
        # Verify the generated key is loadable
        kp = KeyPair.from_file(out)
        assert len(kp.fingerprint) == 64

    def test_fingerprint_private_key(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        import argparse

        from pyfuse.__main__ import _cmd_keypair

        kp = KeyPair.generate()
        path = tmp_path / "key.pem"
        kp.save(path)

        args = argparse.Namespace(keypair_action="fingerprint", key_file=str(path))
        _cmd_keypair(args)
        out = capsys.readouterr().out.strip()
        assert out == kp.fingerprint

    def test_fingerprint_public_key(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        import argparse

        from pyfuse.__main__ import _cmd_keypair

        kp = KeyPair.generate()
        path = tmp_path / "key.pub"
        kp.save_public(path)

        args = argparse.Namespace(keypair_action="fingerprint", key_file=str(path))
        _cmd_keypair(args)
        out = capsys.readouterr().out.strip()
        assert out == kp.fingerprint
