"""Tests for offwork.storage_path() and the storage=True task option."""

import asyncio
from pathlib import Path

import offwork
import pytest

pytest.importorskip("websockets")

from offwork.core.errors import StorageNotSupportedError
from offwork.core.storage import STORAGE_ENV
from offwork.worker.backends.ws import WebSocketBackend
import offwork.worker.remote as _remote


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


def test_ws_backend_supports_persistent_storage() -> None:
    backend = WebSocketBackend("wss://example.com/api/v1/broker/ws")
    assert backend.supports_persistent_storage is True


@pytest.mark.asyncio
async def test_storage_task_rejected_on_local_backend() -> None:
    _remote._active_backend = None
    offwork.connect("local://localhost:9748")

    @offwork.task(storage=True)
    def needs_volume() -> str:
        return offwork.storage_path().as_posix()

    with pytest.raises(StorageNotSupportedError, match="WebSocket"):
        await needs_volume.run()
    _remote._active_backend = None
