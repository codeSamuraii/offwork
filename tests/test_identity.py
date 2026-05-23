"""Tests for offwork.core.identity."""

import stat
from pathlib import Path

import pytest

from offwork.core import identity, ed25519


@pytest.fixture
def key_dir(tmp_path: Path) -> Path:
    return tmp_path / "offwork"


def test_client_id_auto_generates_and_persists(key_dir: Path) -> None:
    cid1 = identity.get_client_id(key_dir)
    assert len(cid1) == 32
    int(cid1, 16)  # valid hex
    cid2 = identity.get_client_id(key_dir)
    assert cid1 == cid2


def test_client_id_file_is_0600(key_dir: Path) -> None:
    identity.get_client_id(key_dir)
    path = key_dir / "client_id"
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_client_id_regenerates_on_corruption(key_dir: Path) -> None:
    path = key_dir / "client_id"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not-valid-hex\n")
    cid = identity.get_client_id(key_dir)
    assert len(cid) == 32
    int(cid, 16)


def test_identity_seed_auto_generates_and_persists(key_dir: Path) -> None:
    s1 = identity.get_identity_seed(key_dir)
    assert len(s1) == 32
    s2 = identity.get_identity_seed(key_dir)
    assert s1 == s2


def test_identity_seed_file_is_0600(key_dir: Path) -> None:
    identity.get_identity_seed(key_dir)
    path = key_dir / "identity.key"
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_identity_seed_regenerates_on_corruption(key_dir: Path) -> None:
    path = key_dir / "identity.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"too-short")
    seed = identity.get_identity_seed(key_dir)
    assert len(seed) == 32


def test_public_key_matches_seed(key_dir: Path) -> None:
    seed = identity.get_identity_seed(key_dir)
    pub = identity.get_public_key(key_dir)
    assert pub == ed25519.seed_to_public(seed)


def test_fingerprint_stable_and_short(key_dir: Path) -> None:
    fp1 = identity.get_identity_fingerprint(key_dir)
    fp2 = identity.get_identity_fingerprint(key_dir)
    assert fp1 == fp2
    assert len(fp1) == 16
    int(fp1, 16)


def test_clear_identity(key_dir: Path) -> None:
    identity.get_client_id(key_dir)
    identity.get_identity_seed(key_dir)
    assert (key_dir / "client_id").exists()
    assert (key_dir / "identity.key").exists()
    assert identity.clear_identity(key_dir) is True
    assert not (key_dir / "client_id").exists()
    assert not (key_dir / "identity.key").exists()
    assert identity.clear_identity(key_dir) is False
