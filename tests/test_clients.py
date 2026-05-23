"""Tests for offwork.core.clients (KnownClients TOFU registry)."""

from pathlib import Path

import pytest

from offwork.core.clients import KnownClients
from offwork.core.errors import IdentityMismatchError


@pytest.fixture
def store(tmp_path: Path) -> KnownClients:
    return KnownClients(key_dir=tmp_path)


def test_tofu_first_then_known(store: KnownClients) -> None:
    assert store.register_or_verify("cid1", "ab" * 32) == "new"
    assert store.register_or_verify("cid1", "ab" * 32) == "known"


def test_identity_mismatch(store: KnownClients) -> None:
    store.register_or_verify("cid1", "ab" * 32)
    with pytest.raises(IdentityMismatchError):
        store.register_or_verify("cid1", "cd" * 32)


def test_persistence_across_instances(tmp_path: Path) -> None:
    s1 = KnownClients(key_dir=tmp_path)
    s1.register_or_verify("cid1", "ab" * 32)
    s2 = KnownClients(key_dir=tmp_path)
    assert s2.register_or_verify("cid1", "ab" * 32) == "known"


def test_revoke_and_approve(store: KnownClients) -> None:
    store.register_or_verify("cid1", "ab" * 32)
    assert store.is_revoked("cid1") is False
    assert store.revoke("cid1") is True
    assert store.is_revoked("cid1") is True
    # idempotent
    assert store.revoke("cid1") is False
    assert store.approve("cid1") is True
    assert store.is_revoked("cid1") is False
    assert store.approve("cid1") is False


def test_revoke_unknown_returns_false(store: KnownClients) -> None:
    assert store.revoke("does-not-exist") is False
    assert store.is_revoked("does-not-exist") is False


def test_list_and_get(store: KnownClients) -> None:
    store.register_or_verify("a", "11" * 32)
    store.register_or_verify("b", "22" * 32)
    listed = store.list_clients()
    assert {e.client_id for e in listed} == {"a", "b"}
    entry = store.get("a")
    assert entry is not None and entry.pubkey == "11" * 32


def test_corrupt_file_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "known_clients.json").write_text("{not-json")
    store = KnownClients(key_dir=tmp_path)
    assert store.list_clients() == []
