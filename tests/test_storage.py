"""Tests for offwork.storage_path()."""

from pathlib import Path

import offwork
from offwork.core.storage import STORAGE_ENV


def test_storage_path_uses_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(STORAGE_ENV, str(tmp_path))
    p = offwork.storage_path()
    assert p == tmp_path.resolve()
    assert p.is_dir()


def test_storage_path_joins_parts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(STORAGE_ENV, str(tmp_path))
    p = offwork.storage_path("models", "weights.bin")
    assert p == (tmp_path / "models" / "weights.bin").resolve()
    # Only the storage root is created, not joined subdirectories.
    assert tmp_path.is_dir()
    assert not p.parent.exists()


def test_storage_path_default_when_unset(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(STORAGE_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    p = offwork.storage_path()
    assert p == (tmp_path / "offwork-storage").resolve()
    assert p.is_dir()


def test_storage_path_in_public_api() -> None:
    assert "storage_path" in offwork.__all__
    assert callable(offwork.storage_path)
