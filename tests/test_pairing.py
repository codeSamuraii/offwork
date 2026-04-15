"""Tests for pyfuse.core.pairing — SPAKE2-based automated pairing."""

import asyncio
from pathlib import Path

import pytest

from pyfuse.core.pairing import (
    MemoryPairingTransport,
    PairingResult,
    _channel_prefix,
    _decrypt,
    _encrypt,
    accept_pairing,
    generate_pairing_code,
    request_pairing,
)
from pyfuse.core.signing import KeyPair, TrustStore, _fingerprint


# ===========================================================================
# Helpers
# ===========================================================================


class TestGeneratePairingCode:
    def test_default_length(self) -> None:
        code = generate_pairing_code()
        assert len(code) == 6
        assert code.isdigit()

    def test_custom_length(self) -> None:
        code = generate_pairing_code(length=8)
        assert len(code) == 8

    def test_codes_are_unique(self) -> None:
        codes = {generate_pairing_code() for _ in range(50)}
        assert len(codes) > 1  # extremely unlikely to collide 50 times


class TestChannelPrefix:
    def test_deterministic(self) -> None:
        assert _channel_prefix("123456") == _channel_prefix("123456")

    def test_different_codes_different_prefixes(self) -> None:
        assert _channel_prefix("111111") != _channel_prefix("222222")

    def test_starts_with_namespace(self) -> None:
        assert _channel_prefix("000000").startswith("pyfuse:pair:")


class TestEncryptDecrypt:
    def test_roundtrip(self) -> None:
        key = b"a" * 32
        plaintext = b"hello world"
        ct = _encrypt(key, plaintext)
        assert _decrypt(key, ct) == plaintext

    def test_wrong_key_fails(self) -> None:
        ct = _encrypt(b"key1" + b"\x00" * 28, b"data")
        with pytest.raises(Exception):
            _decrypt(b"key2" + b"\x00" * 28, ct)

    def test_ciphertext_is_different_each_time(self) -> None:
        key = b"k" * 32
        ct1 = _encrypt(key, b"data")
        ct2 = _encrypt(key, b"data")
        assert ct1 != ct2  # random nonce


# ===========================================================================
# MemoryPairingTransport
# ===========================================================================


class TestMemoryPairingTransport:
    @pytest.mark.asyncio
    async def test_put_and_get(self) -> None:
        t = MemoryPairingTransport()
        await t.put("key", b"value")
        result = await t.get("key", timeout=1.0)
        assert result == b"value"

    @pytest.mark.asyncio
    async def test_get_timeout(self) -> None:
        t = MemoryPairingTransport()
        result = await t.get("missing", timeout=0.1)
        assert result is None

    @pytest.mark.asyncio
    async def test_concurrent_put_get(self) -> None:
        t = MemoryPairingTransport()

        async def writer() -> None:
            await asyncio.sleep(0.05)
            await t.put("key", b"hello")

        async def reader() -> bytes | None:
            return await t.get("key", timeout=2.0)

        results = await asyncio.gather(writer(), reader())
        assert results[1] == b"hello"


# ===========================================================================
# Full pairing protocol
# ===========================================================================


class TestPairingProtocol:
    @pytest.mark.asyncio
    async def test_successful_pairing(self) -> None:
        """Both sides agree on the code → pairing succeeds."""
        transport = MemoryPairingTransport()
        code = "123456"

        async def worker_side() -> PairingResult:
            return await accept_pairing(transport, code, timeout=5.0)

        async def client_side() -> PairingResult:
            return await request_pairing(transport, code, timeout=5.0)

        worker_result, client_result = await asyncio.gather(
            worker_side(), client_side(),
        )

        assert worker_result.fingerprint == client_result.fingerprint
        assert len(worker_result.public_key_bytes) == 32
        assert worker_result.public_key_bytes == client_result.public_key_bytes

    @pytest.mark.asyncio
    async def test_pairing_with_existing_keypair(self) -> None:
        """Client can supply an existing keypair."""
        transport = MemoryPairingTransport()
        code = "654321"
        kp = KeyPair.generate()

        async def worker_side() -> PairingResult:
            return await accept_pairing(transport, code, timeout=5.0)

        async def client_side() -> PairingResult:
            return await request_pairing(
                transport, code, keypair=kp, timeout=5.0,
            )

        worker_result, client_result = await asyncio.gather(
            worker_side(), client_side(),
        )

        assert worker_result.fingerprint == kp.fingerprint
        assert client_result.fingerprint == kp.fingerprint

    @pytest.mark.asyncio
    async def test_wrong_code_fails(self) -> None:
        """Mismatched codes → decryption fails on worker side."""
        transport = MemoryPairingTransport()

        async def worker_side() -> PairingResult:
            return await accept_pairing(transport, "111111", timeout=5.0)

        async def client_side() -> PairingResult:
            return await request_pairing(transport, "222222", timeout=5.0)

        # With mismatched codes, SPAKE2 messages go to different channels.
        # Both sides will time out since they listen on different prefixes.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(worker_side(), client_side()),
                timeout=1.0,
            )

    @pytest.mark.asyncio
    async def test_pairing_saves_key_to_directory(self, tmp_path: Path) -> None:
        """Worker persists the .pub file when trusted_keys_dir is given."""
        transport = MemoryPairingTransport()
        code = "999888"
        keys_dir = tmp_path / "trusted_keys"

        async def worker_side() -> PairingResult:
            return await accept_pairing(
                transport, code,
                trusted_keys_dir=keys_dir,
                timeout=5.0,
            )

        async def client_side() -> PairingResult:
            return await request_pairing(transport, code, timeout=5.0)

        worker_result, _ = await asyncio.gather(
            worker_side(), client_side(),
        )

        # Verify .pub file was created
        pub_files = list(keys_dir.glob("*.pub"))
        assert len(pub_files) == 1
        assert worker_result.fingerprint[:16] in pub_files[0].name

        # Verify the key is loadable by TrustStore
        ts = TrustStore.from_directory(keys_dir)
        assert ts.is_trusted(worker_result.fingerprint)

    @pytest.mark.asyncio
    async def test_pairing_saves_client_keypair(self, tmp_path: Path) -> None:
        """Client saves its keypair when save_path is provided."""
        transport = MemoryPairingTransport()
        code = "777666"
        key_path = tmp_path / "my_key.pem"

        async def worker_side() -> PairingResult:
            return await accept_pairing(transport, code, timeout=5.0)

        async def client_side() -> PairingResult:
            return await request_pairing(
                transport, code, save_path=key_path, timeout=5.0,
            )

        _, client_result = await asyncio.gather(
            worker_side(), client_side(),
        )

        assert key_path.exists()
        assert key_path.with_suffix(".pub").exists()

        # Verify saved key matches
        loaded = KeyPair.from_file(key_path)
        assert loaded.fingerprint == client_result.fingerprint


# ===========================================================================
# End-to-end: pairing → task signing → worker trust
# ===========================================================================


class TestPairingEndToEnd:
    @pytest.mark.asyncio
    async def test_paired_key_works_for_signing(self, tmp_path: Path) -> None:
        """A key enrolled via pairing is accepted by a TrustStore."""
        transport = MemoryPairingTransport()
        code = "424242"
        keys_dir = tmp_path / "keys"
        kp = KeyPair.generate()

        async def worker_side() -> PairingResult:
            return await accept_pairing(
                transport, code, trusted_keys_dir=keys_dir, timeout=5.0,
            )

        async def client_side() -> PairingResult:
            return await request_pairing(
                transport, code, keypair=kp, timeout=5.0,
            )

        await asyncio.gather(worker_side(), client_side())

        # Load the trust store from the directory
        ts = TrustStore.from_directory(keys_dir)
        assert ts.is_trusted(kp.fingerprint)

        # Sign data and verify against the trust store
        data = b"task payload"
        sig = kp.sign(data)
        assert ts.verify(data, sig, kp.public_bytes)

    @pytest.mark.asyncio
    async def test_worker_timeout(self) -> None:
        """Worker times out when no client connects."""
        transport = MemoryPairingTransport()
        with pytest.raises(TimeoutError, match="client"):
            await accept_pairing(transport, "000000", timeout=0.1)

    @pytest.mark.asyncio
    async def test_client_timeout(self) -> None:
        """Client times out when no worker is listening."""
        transport = MemoryPairingTransport()
        with pytest.raises(TimeoutError, match="worker"):
            await request_pairing(transport, "000000", timeout=0.1)


# ===========================================================================
# CLI pair command
# ===========================================================================


class TestCLIPair:
    def test_pair_subcommand_exists(self) -> None:
        from pyfuse.__main__ import _build_parser

        parser = _build_parser()
        # Should not raise
        args = parser.parse_args(["pair", "accept", "--backend", "redis://x", "--code", "123"])
        assert args.pair_action == "accept"
        assert args.code == "123"

    def test_pair_request_subcommand(self) -> None:
        from pyfuse.__main__ import _build_parser

        parser = _build_parser()
        args = parser.parse_args([
            "pair", "request", "--backend", "redis://x", "--code", "456", "-o", "key.pem",
        ])
        assert args.pair_action == "request"
        assert args.code == "456"
        assert args.output == "key.pem"
