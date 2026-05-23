"""Tests for offwork.core.signing primitives (HMAC + NonceLRU)."""

import time

from offwork.core.signing import (
    NonceLRU,
    compute_signature,
    derive_key,
    verify_signature,
)


class TestComputeSignature:
    def test_deterministic(self) -> None:
        key = b"test-secret-key-32bytes-long!!!!!"
        assert compute_signature("hello", key) == compute_signature("hello", key)

    def test_different_payload(self) -> None:
        key = b"k"
        assert compute_signature("hello", key) != compute_signature("world", key)

    def test_different_key(self) -> None:
        assert compute_signature("hello", b"k1") != compute_signature("hello", b"k2")

    def test_hex_64(self) -> None:
        sig = compute_signature("data", b"key")
        assert len(sig) == 64
        int(sig, 16)


class TestVerifySignature:
    def test_valid(self) -> None:
        key = b"s"
        sig = compute_signature("payload", key)
        assert verify_signature("payload", sig, key) is True

    def test_invalid(self) -> None:
        assert verify_signature("payload", "0" * 64, b"s") is False

    def test_wrong_key(self) -> None:
        sig = compute_signature("payload", b"k1")
        assert verify_signature("payload", sig, b"k2") is False

    def test_tampered(self) -> None:
        key = b"s"
        sig = compute_signature("original", key)
        assert verify_signature("tampered", sig, key) is False


class TestDeriveKey:
    def test_deterministic(self) -> None:
        assert derive_key(b"x") == derive_key(b"x")

    def test_different_secrets(self) -> None:
        assert derive_key(b"a") != derive_key(b"b")

    def test_different_contexts(self) -> None:
        assert derive_key(b"x", "c1") != derive_key(b"x", "c2")

    def test_32_bytes(self) -> None:
        k = derive_key(b"s")
        assert isinstance(k, bytes) and len(k) == 32


class TestNonceLRU:
    def test_first_seen_accepted(self) -> None:
        lru = NonceLRU(ttl=60.0)
        assert lru.check_and_add("c1", "n1") is True

    def test_replay_rejected(self) -> None:
        lru = NonceLRU(ttl=60.0)
        assert lru.check_and_add("c1", "n1") is True
        assert lru.check_and_add("c1", "n1") is False

    def test_same_nonce_different_client_ok(self) -> None:
        lru = NonceLRU(ttl=60.0)
        assert lru.check_and_add("c1", "n1") is True
        assert lru.check_and_add("c2", "n1") is True

    def test_ttl_eviction(self) -> None:
        lru = NonceLRU(ttl=10.0)
        t0 = time.time()
        assert lru.check_and_add("c1", "n1", now=t0) is True
        # Same nonce 5s later: still rejected
        assert lru.check_and_add("c1", "n1", now=t0 + 5) is False
        # After TTL: accepted again
        assert lru.check_and_add("c1", "n1", now=t0 + 20) is True

    def test_capacity_eviction(self) -> None:
        lru = NonceLRU(ttl=3600.0, capacity=3)
        for i in range(5):
            assert lru.check_and_add("c", f"n{i}") is True
        # Oldest entries dropped — n0 is fresh again
        assert lru.check_and_add("c", "n0") is True
        # Most recent (n4) is still cached
        assert lru.check_and_add("c", "n4") is False

    def test_len(self) -> None:
        lru = NonceLRU(ttl=60.0)
        lru.check_and_add("c", "a")
        lru.check_and_add("c", "b")
        assert len(lru) == 2
