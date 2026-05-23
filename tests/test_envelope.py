"""Tests for offwork.core.envelope: signed envelope build + verify."""

import json
import time
from pathlib import Path

import pytest

from offwork.core import ed25519
from offwork.core.task import Task
from offwork.core.signing import NonceLRU
from offwork.core.clients import KnownClients
from offwork.core.envelope import (
    DEFAULT_CLOCK_SKEW,
    build_signed_envelope,
    verify_task_envelope,
)
from offwork.core.errors import (
    ReplayError,
    StaleTaskError,
    SignatureError,
    ClientRevokedError,
    IdentityMismatchError,
)


@pytest.fixture
def root_token() -> bytes:
    return b"\x42" * 32


@pytest.fixture
def identity() -> tuple[str, bytes, bytes]:
    seed = ed25519.generate_seed()
    pub = ed25519.seed_to_public(seed)
    return ("c1" * 16, seed, pub)


@pytest.fixture
def store(tmp_path: Path) -> KnownClients:
    return KnownClients(key_dir=tmp_path)


@pytest.fixture
def lru() -> NonceLRU:
    return NonceLRU(ttl=60.0)


def _sample_task() -> Task:
    return Task(graph_json="{}", function_name="m.f", args=(1, 2), kwargs={"k": "v"})


def test_round_trip(
    root_token: bytes,
    identity: tuple[str, bytes, bytes],
    store: KnownClients,
    lru: NonceLRU,
) -> None:
    cid, seed, pub = identity
    env = build_signed_envelope(
        _sample_task(),
        root_token=root_token,
        client_id=cid,
        identity_seed=seed,
        public_key=pub,
    )
    task = verify_task_envelope(
        env, root_token=root_token, known_clients=store, nonce_lru=lru,
    )
    assert task.function_name == "m.f"
    assert task.args == (1, 2)
    assert task.kwargs == {"k": "v"}


def test_tofu_first_then_pinned(
    root_token: bytes,
    identity: tuple[str, bytes, bytes],
    store: KnownClients,
    lru: NonceLRU,
) -> None:
    cid, seed, pub = identity
    env1 = build_signed_envelope(
        _sample_task(),
        root_token=root_token, client_id=cid,
        identity_seed=seed, public_key=pub,
    )
    verify_task_envelope(
        env1, root_token=root_token, known_clients=store, nonce_lru=lru,
    )
    assert store.get(cid) is not None
    # Second submission with same identity → succeeds
    env2 = build_signed_envelope(
        _sample_task(),
        root_token=root_token, client_id=cid,
        identity_seed=seed, public_key=pub,
    )
    verify_task_envelope(
        env2, root_token=root_token, known_clients=store, nonce_lru=lru,
    )


def test_identity_mismatch_after_pin(
    root_token: bytes,
    identity: tuple[str, bytes, bytes],
    store: KnownClients,
    lru: NonceLRU,
) -> None:
    cid, seed, pub = identity
    env = build_signed_envelope(
        _sample_task(),
        root_token=root_token, client_id=cid,
        identity_seed=seed, public_key=pub,
    )
    verify_task_envelope(
        env, root_token=root_token, known_clients=store, nonce_lru=lru,
    )
    # Different seed → different pubkey → reject
    other_seed = ed25519.generate_seed()
    other_pub = ed25519.seed_to_public(other_seed)
    env2 = build_signed_envelope(
        _sample_task(),
        root_token=root_token, client_id=cid,
        identity_seed=other_seed, public_key=other_pub,
    )
    with pytest.raises(IdentityMismatchError):
        verify_task_envelope(
            env2, root_token=root_token, known_clients=store, nonce_lru=lru,
        )


def test_replay_rejected(
    root_token: bytes,
    identity: tuple[str, bytes, bytes],
    store: KnownClients,
    lru: NonceLRU,
) -> None:
    cid, seed, pub = identity
    env = build_signed_envelope(
        _sample_task(),
        root_token=root_token, client_id=cid,
        identity_seed=seed, public_key=pub,
    )
    verify_task_envelope(
        env, root_token=root_token, known_clients=store, nonce_lru=lru,
    )
    with pytest.raises(ReplayError):
        verify_task_envelope(
            env, root_token=root_token, known_clients=store, nonce_lru=lru,
        )


def test_stale_iat_rejected(
    root_token: bytes,
    identity: tuple[str, bytes, bytes],
    store: KnownClients,
    lru: NonceLRU,
) -> None:
    cid, seed, pub = identity
    env = build_signed_envelope(
        _sample_task(),
        root_token=root_token, client_id=cid,
        identity_seed=seed, public_key=pub,
        now=time.time() - (DEFAULT_CLOCK_SKEW + 100),
    )
    with pytest.raises(StaleTaskError):
        verify_task_envelope(
            env, root_token=root_token, known_clients=store, nonce_lru=lru,
        )


def test_future_iat_rejected(
    root_token: bytes,
    identity: tuple[str, bytes, bytes],
    store: KnownClients,
    lru: NonceLRU,
) -> None:
    cid, seed, pub = identity
    env = build_signed_envelope(
        _sample_task(),
        root_token=root_token, client_id=cid,
        identity_seed=seed, public_key=pub,
        now=time.time() + (DEFAULT_CLOCK_SKEW + 100),
    )
    with pytest.raises(StaleTaskError):
        verify_task_envelope(
            env, root_token=root_token, known_clients=store, nonce_lru=lru,
        )


def test_revoked_client_rejected(
    root_token: bytes,
    identity: tuple[str, bytes, bytes],
    store: KnownClients,
    lru: NonceLRU,
) -> None:
    cid, seed, pub = identity
    env = build_signed_envelope(
        _sample_task(),
        root_token=root_token, client_id=cid,
        identity_seed=seed, public_key=pub,
    )
    verify_task_envelope(
        env, root_token=root_token, known_clients=store, nonce_lru=lru,
    )
    store.revoke(cid)
    env2 = build_signed_envelope(
        _sample_task(),
        root_token=root_token, client_id=cid,
        identity_seed=seed, public_key=pub,
    )
    with pytest.raises(ClientRevokedError):
        verify_task_envelope(
            env2, root_token=root_token, known_clients=store, nonce_lru=lru,
        )


def test_tampered_body_rejected(
    root_token: bytes,
    identity: tuple[str, bytes, bytes],
    store: KnownClients,
    lru: NonceLRU,
) -> None:
    cid, seed, pub = identity
    env = build_signed_envelope(
        _sample_task(),
        root_token=root_token, client_id=cid,
        identity_seed=seed, public_key=pub,
    )
    data = json.loads(env)
    data["function"] = "evil.code"
    tampered = json.dumps(data)
    with pytest.raises(SignatureError):
        verify_task_envelope(
            tampered, root_token=root_token, known_clients=store, nonce_lru=lru,
        )


def test_wrong_token_rejected(
    identity: tuple[str, bytes, bytes],
    store: KnownClients,
    lru: NonceLRU,
) -> None:
    cid, seed, pub = identity
    env = build_signed_envelope(
        _sample_task(),
        root_token=b"\x01" * 32, client_id=cid,
        identity_seed=seed, public_key=pub,
    )
    with pytest.raises(SignatureError):
        verify_task_envelope(
            env, root_token=b"\x02" * 32, known_clients=store, nonce_lru=lru,
        )


def test_tampered_ed_signature_rejected(
    root_token: bytes,
    identity: tuple[str, bytes, bytes],
    store: KnownClients,
    lru: NonceLRU,
) -> None:
    cid, seed, pub = identity
    env = build_signed_envelope(
        _sample_task(),
        root_token=root_token, client_id=cid,
        identity_seed=seed, public_key=pub,
    )
    data = json.loads(env)
    # Flip one bit of the Ed25519 signature.  The HMAC must also be
    # recomputed to bypass the earlier check.
    bad = bytearray(bytes.fromhex(data["ed_sig"]))
    bad[0] ^= 0x01
    data["ed_sig"] = bytes(bad).hex()
    # Re-HMAC the body so the HMAC check passes and we hit Ed25519.
    from offwork.core.envelope import _canonical_payload, per_client_key
    from offwork.core.signing import compute_signature
    payload = _canonical_payload(data)
    data["signature"] = compute_signature(payload, per_client_key(root_token, cid))
    with pytest.raises(SignatureError):
        verify_task_envelope(
            json.dumps(data),
            root_token=root_token, known_clients=store, nonce_lru=lru,
        )


def test_missing_fields_rejected(
    root_token: bytes,
    store: KnownClients,
    lru: NonceLRU,
) -> None:
    with pytest.raises(SignatureError):
        verify_task_envelope(
            "{}", root_token=root_token, known_clients=store, nonce_lru=lru,
        )
