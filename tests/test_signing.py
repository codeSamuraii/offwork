import json

import pytest

from seeya.core.errors import SignatureError
from seeya.core.signing import (
    compute_signature,
    derive_key,
    sign_json,
    verify_and_load_json,
    verify_signature,
)
from seeya.core.task import Task


class TestComputeSignature:
    def test_deterministic(self) -> None:
        key = b"test-secret-key-32bytes-long!!!!!"
        sig1 = compute_signature("hello", key)
        sig2 = compute_signature("hello", key)
        assert sig1 == sig2

    def test_different_payload(self) -> None:
        key = b"test-secret-key-32bytes-long!!!!!"
        sig1 = compute_signature("hello", key)
        sig2 = compute_signature("world", key)
        assert sig1 != sig2

    def test_different_key(self) -> None:
        sig1 = compute_signature("hello", b"key-one-xxxxxxxxxxxxxxxxx")
        sig2 = compute_signature("hello", b"key-two-xxxxxxxxxxxxxxxxx")
        assert sig1 != sig2

    def test_returns_hex_string(self) -> None:
        sig = compute_signature("data", b"key")
        assert isinstance(sig, str)
        assert len(sig) == 64  # SHA-256 hex digest
        int(sig, 16)  # should not raise


class TestVerifySignature:
    def test_valid(self) -> None:
        key = b"shared-secret"
        sig = compute_signature("payload", key)
        assert verify_signature("payload", sig, key) is True

    def test_invalid_signature(self) -> None:
        key = b"shared-secret"
        assert verify_signature("payload", "0" * 64, key) is False

    def test_wrong_key(self) -> None:
        key1 = b"key-one"
        key2 = b"key-two"
        sig = compute_signature("payload", key1)
        assert verify_signature("payload", sig, key2) is False

    def test_tampered_payload(self) -> None:
        key = b"shared-secret"
        sig = compute_signature("original", key)
        assert verify_signature("tampered", sig, key) is False


class TestSignJson:
    def test_roundtrip(self) -> None:
        key = b"test-key-for-signing"
        data = {"name": "test", "value": 42}
        envelope = sign_json(data, key)
        result = verify_and_load_json(envelope, key)
        assert result == data

    def test_envelope_structure(self) -> None:
        key = b"key"
        envelope = sign_json({"a": 1}, key)
        parsed = json.loads(envelope)
        assert "payload" in parsed
        assert "signature" in parsed
        # payload is a JSON string
        inner = json.loads(parsed["payload"])
        assert inner == {"a": 1}

    def test_wrong_key_fails(self) -> None:
        envelope = sign_json({"x": 1}, b"key-one")
        with pytest.raises(SignatureError, match="signature verification failed"):
            verify_and_load_json(envelope, b"key-two")

    def test_tampered_payload_fails(self) -> None:
        key = b"key"
        envelope = sign_json({"x": 1}, key)
        # Tamper with the payload
        parsed = json.loads(envelope)
        parsed["payload"] = '{"x":999}'
        tampered = json.dumps(parsed)
        with pytest.raises(SignatureError, match="signature verification failed"):
            verify_and_load_json(tampered, key)

    def test_missing_signature_field(self) -> None:
        key = b"key"
        with pytest.raises(SignatureError, match="missing"):
            verify_and_load_json('{"payload": "{}"}', key)

    def test_invalid_json(self) -> None:
        with pytest.raises(SignatureError, match="Invalid"):
            verify_and_load_json("not-json", b"key")


class TestDeriveKey:
    def test_deterministic(self) -> None:
        secret = b"shared-secret"
        k1 = derive_key(secret)
        k2 = derive_key(secret)
        assert k1 == k2

    def test_different_secrets(self) -> None:
        k1 = derive_key(b"secret-one")
        k2 = derive_key(b"secret-two")
        assert k1 != k2

    def test_different_contexts(self) -> None:
        secret = b"same-secret"
        k1 = derive_key(secret, "context-a")
        k2 = derive_key(secret, "context-b")
        assert k1 != k2

    def test_returns_32_bytes(self) -> None:
        key = derive_key(b"secret")
        assert isinstance(key, bytes)
        assert len(key) == 32


class TestTaskSigning:
    """Test Task.to_json / Task.from_json with signing."""

    def _key(self) -> bytes:
        return derive_key(b"test-shared-secret")

    def test_signed_roundtrip(self) -> None:
        key = self._key()
        task = Task(
            graph_json='{"objects": {}}',
            function_name="m.func",
            args=(1, "two"),
            kwargs={"k": "v"},
            task_id="test123",
        )
        signed = task.to_json(signing_key=key)
        restored = Task.from_json(signed, signing_key=key)
        assert restored.task_id == "test123"
        assert restored.function_name == "m.func"
        assert restored.args == (1, "two")
        assert restored.kwargs == {"k": "v"}
        assert restored.signature is not None

    def test_signature_in_json(self) -> None:
        key = self._key()
        task = Task(graph_json="{}", function_name="f", task_id="t")
        signed = task.to_json(signing_key=key)
        data = json.loads(signed)
        assert "signature" in data
        assert isinstance(data["signature"], str)
        assert len(data["signature"]) == 64

    def test_unsigned_task_accepted_without_key(self) -> None:
        """Without a signing key, unsigned tasks are accepted normally."""
        task = Task(graph_json="{}", function_name="f", task_id="t")
        raw = task.to_json()
        restored = Task.from_json(raw)
        assert restored.task_id == "t"
        assert restored.signature is None

    def test_unsigned_task_rejected_with_key(self) -> None:
        """When signing is enabled, unsigned tasks are rejected."""
        key = self._key()
        task = Task(graph_json="{}", function_name="f")
        raw = task.to_json()  # no signing key
        with pytest.raises(SignatureError, match="unsigned"):
            Task.from_json(raw, signing_key=key)

    def test_wrong_key_rejected(self) -> None:
        key1 = derive_key(b"secret-one")
        key2 = derive_key(b"secret-two")
        task = Task(graph_json="{}", function_name="f")
        signed = task.to_json(signing_key=key1)
        with pytest.raises(SignatureError, match="verification failed"):
            Task.from_json(signed, signing_key=key2)

    def test_tampered_graph_rejected(self) -> None:
        key = self._key()
        task = Task(graph_json='{"clean": true}', function_name="f")
        signed = task.to_json(signing_key=key)
        # Tamper with the graph field
        data = json.loads(signed)
        data["graph"] = '{"malicious": true}'
        tampered = json.dumps(data)
        with pytest.raises(SignatureError, match="verification failed"):
            Task.from_json(tampered, signing_key=key)

    def test_tampered_function_name_rejected(self) -> None:
        key = self._key()
        task = Task(graph_json="{}", function_name="safe.func")
        signed = task.to_json(signing_key=key)
        data = json.loads(signed)
        data["function"] = "evil.func"
        tampered = json.dumps(data)
        with pytest.raises(SignatureError, match="verification failed"):
            Task.from_json(tampered, signing_key=key)

    def test_backward_compatible_unsigned(self) -> None:
        """Existing unsigned tasks work when no key is provided."""
        task = Task(
            graph_json='{"g": 1}',
            function_name="f",
            args=(1,),
            task_id="old",
        )
        raw = task.to_json()
        restored = Task.from_json(raw)
        assert restored.task_id == "old"
        assert restored.signature is None
