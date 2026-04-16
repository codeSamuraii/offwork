import asyncio
import json
from pathlib import Path

import pytest

from pyfuse.core.errors import PairingError
from pyfuse.core.pairing import (
    PairingResult,
    _derive_intermediate,
    clear_shared_key,
    compute_response,
    derive_shared_secret,
    generate_challenge,
    generate_pin,
    initiate_pairing,
    load_shared_key,
    make_challenge_message,
    make_confirm_message,
    make_response_message,
    parse_challenge_message,
    parse_confirm_message,
    parse_response_message,
    respond_to_pairing,
    save_shared_key,
    verify_response,
)


class TestGeneratePin:
    def test_length(self) -> None:
        pin = generate_pin()
        assert len(pin) == 6

    def test_numeric(self) -> None:
        pin = generate_pin()
        assert pin.isdigit()

    def test_zero_padded(self) -> None:
        """Ensure PINs like '000042' keep their leading zeros."""
        # Generate many PINs and check they all have correct length
        for _ in range(100):
            pin = generate_pin()
            assert len(pin) == 6

    def test_unique(self) -> None:
        pins = {generate_pin() for _ in range(50)}
        # With 6-digit PINs, 50 should almost always be unique
        assert len(pins) > 40


class TestDeriveIntermediate:
    def test_deterministic(self) -> None:
        k1 = _derive_intermediate("123456")
        k2 = _derive_intermediate("123456")
        assert k1 == k2

    def test_different_pins(self) -> None:
        k1 = _derive_intermediate("123456")
        k2 = _derive_intermediate("654321")
        assert k1 != k2

    def test_returns_32_bytes(self) -> None:
        key = _derive_intermediate("000000")
        assert isinstance(key, bytes)
        assert len(key) == 32


class TestChallengeResponse:
    def test_challenge_length(self) -> None:
        challenge = generate_challenge()
        assert len(challenge) == 32

    def test_response_deterministic(self) -> None:
        intermediate = _derive_intermediate("111111")
        challenge = b"\x00" * 32
        r1 = compute_response(intermediate, challenge)
        r2 = compute_response(intermediate, challenge)
        assert r1 == r2

    def test_verify_correct(self) -> None:
        intermediate = _derive_intermediate("222222")
        challenge = generate_challenge()
        response = compute_response(intermediate, challenge)
        assert verify_response(intermediate, challenge, response) is True

    def test_verify_wrong_pin(self) -> None:
        challenge = generate_challenge()
        correct = _derive_intermediate("111111")
        wrong = _derive_intermediate("999999")
        response = compute_response(wrong, challenge)
        assert verify_response(correct, challenge, response) is False

    def test_verify_wrong_challenge(self) -> None:
        intermediate = _derive_intermediate("333333")
        c1 = b"\x01" * 32
        c2 = b"\x02" * 32
        response = compute_response(intermediate, c1)
        assert verify_response(intermediate, c2, response) is False


class TestDeriveSharedSecret:
    def test_deterministic(self) -> None:
        intermediate = _derive_intermediate("444444")
        challenge = b"\xAB" * 32
        s1 = derive_shared_secret(intermediate, challenge)
        s2 = derive_shared_secret(intermediate, challenge)
        assert s1 == s2

    def test_both_sides_agree(self) -> None:
        """Both the initiator and responder derive the same secret."""
        pin = "555555"
        intermediate = _derive_intermediate(pin)
        challenge = generate_challenge()
        # Both sides call the same function with the same inputs
        secret1 = derive_shared_secret(intermediate, challenge)
        secret2 = derive_shared_secret(intermediate, challenge)
        assert secret1 == secret2

    def test_returns_32_bytes(self) -> None:
        secret = derive_shared_secret(b"\x00" * 32, b"\x00" * 32)
        assert isinstance(secret, bytes)
        assert len(secret) == 32


class TestMessageSerialization:
    def test_challenge_roundtrip(self) -> None:
        challenge = generate_challenge()
        msg = make_challenge_message(challenge)
        parsed = parse_challenge_message(msg)
        assert parsed == challenge

    def test_response_roundtrip(self) -> None:
        response = b"\xDE\xAD\xBE\xEF" * 8
        msg = make_response_message(response)
        parsed = parse_response_message(msg)
        assert parsed == response

    def test_confirm_roundtrip(self) -> None:
        msg = make_confirm_message()
        parse_confirm_message(msg)  # should not raise

    def test_challenge_wrong_type(self) -> None:
        msg = json.dumps({"type": "pairing_response", "challenge": "00" * 32})
        with pytest.raises(ValueError, match="Expected pairing_challenge"):
            parse_challenge_message(msg)

    def test_response_wrong_type(self) -> None:
        msg = json.dumps({"type": "pairing_challenge", "response": "00" * 32})
        with pytest.raises(ValueError, match="Expected pairing_response"):
            parse_response_message(msg)

    def test_confirm_wrong_type(self) -> None:
        msg = json.dumps({"type": "other"})
        with pytest.raises(ValueError, match="Expected pairing_confirmed"):
            parse_confirm_message(msg)


class TestKeyPersistence:
    def test_save_and_load_client(self, tmp_path: Path) -> None:
        key = b"\x42" * 32
        save_shared_key(key, "client", key_dir=tmp_path)
        loaded = load_shared_key("client", key_dir=tmp_path)
        assert loaded == key

    def test_save_and_load_worker(self, tmp_path: Path) -> None:
        key = b"\x99" * 32
        save_shared_key(key, "worker", key_dir=tmp_path)
        loaded = load_shared_key("worker", key_dir=tmp_path)
        assert loaded == key

    def test_load_missing(self, tmp_path: Path) -> None:
        assert load_shared_key("client", key_dir=tmp_path) is None

    def test_clear_existing(self, tmp_path: Path) -> None:
        save_shared_key(b"\x00" * 32, "client", key_dir=tmp_path)
        assert clear_shared_key("client", key_dir=tmp_path) is True
        assert load_shared_key("client", key_dir=tmp_path) is None

    def test_clear_missing(self, tmp_path: Path) -> None:
        assert clear_shared_key("worker", key_dir=tmp_path) is False

    def test_invalid_key_length(self, tmp_path: Path) -> None:
        path = tmp_path / "client.key"
        path.write_bytes(b"short")
        assert load_shared_key("client", key_dir=tmp_path) is None

    def test_file_permissions(self, tmp_path: Path) -> None:
        save_shared_key(b"\x00" * 32, "worker", key_dir=tmp_path)
        path = tmp_path / "worker.key"
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600


class _MockBackend:
    """Minimal mock backend for pairing protocol tests."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def send_progress(self, key: str, value: str) -> None:
        self._store[key] = value

    async def get_progress(self, key: str) -> str | None:
        return self._store.get(key)


class TestPairingProtocol:
    @pytest.mark.asyncio
    async def test_successful_pairing(self) -> None:
        """Initiator and responder with the same PIN should pair."""
        backend = _MockBackend()
        pin = "123456"

        async def initiator() -> PairingResult:
            return await initiate_pairing(backend, pin, timeout=5.0)

        async def responder() -> PairingResult:
            # Small delay to let initiator publish the challenge first
            await asyncio.sleep(0.1)
            return await respond_to_pairing(backend, pin, timeout=5.0)

        init_result, resp_result = await asyncio.gather(
            initiator(), responder()
        )

        # Both sides should derive the same shared key
        assert init_result.shared_key == resp_result.shared_key
        assert len(init_result.shared_key) == 32
        assert init_result.peer_role == "client"
        assert resp_result.peer_role == "worker"

    @pytest.mark.asyncio
    async def test_wrong_pin_fails(self) -> None:
        """Mismatched PINs should cause pairing failure."""
        backend = _MockBackend()

        async def initiator() -> PairingResult:
            return await initiate_pairing(backend, "111111", timeout=3.0)

        async def responder() -> None:
            await asyncio.sleep(0.1)
            # Respond with wrong PIN
            intermediate = _derive_intermediate("999999")
            raw = await backend.get_progress("pyfuse:pairing")
            assert raw is not None
            challenge = parse_challenge_message(raw)
            response = compute_response(intermediate, challenge)
            msg = make_response_message(response)
            await backend.send_progress("pyfuse:pairing:response", msg)

        with pytest.raises(PairingError, match="PIN mismatch"):
            await asyncio.gather(initiator(), responder())

    @pytest.mark.asyncio
    async def test_initiator_timeout(self) -> None:
        """Initiator should timeout if no response arrives."""
        backend = _MockBackend()
        with pytest.raises(PairingError, match="timed out"):
            await initiate_pairing(backend, "000000", timeout=1.0)

    @pytest.mark.asyncio
    async def test_responder_timeout(self) -> None:
        """Responder should timeout if no challenge arrives."""
        backend = _MockBackend()
        with pytest.raises(PairingError, match="timed out"):
            await respond_to_pairing(backend, "000000", timeout=1.0)
