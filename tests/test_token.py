"""Tests for offwork.core.token: token generation, persistence, root resolution."""

import os
from pathlib import Path
from unittest import mock

import pytest

from offwork.core.token import (
    _TOKEN_ENV_VAR,
    _is_valid_token_hex,
    _validate_token_hex,
    clear_token,
    generate_token,
    load_token,
    resolve_root_token,
    save_token,
)


class TestGenerateToken:
    def test_length(self) -> None:
        assert len(generate_token()) == 64

    def test_hex(self) -> None:
        bytes.fromhex(generate_token())

    def test_unique(self) -> None:
        assert len({generate_token() for _ in range(50)}) == 50


class TestValidation:
    def test_valid(self) -> None:
        assert _is_valid_token_hex("ab" * 32) is True

    def test_too_short(self) -> None:
        assert _is_valid_token_hex("ab" * 16) is False

    def test_not_hex(self) -> None:
        assert _is_valid_token_hex("zz" * 32) is False

    def test_validate_raises(self) -> None:
        with pytest.raises(ValueError, match="64-character hex"):
            _validate_token_hex("too-short")


class TestSaveAndLoad:
    def test_save_and_load(self, tmp_path: Path) -> None:
        token = generate_token()
        save_token(token, key_dir=tmp_path)
        assert load_token(key_dir=tmp_path) == token

    def test_file_permissions(self, tmp_path: Path) -> None:
        save_token(generate_token(), key_dir=tmp_path)
        mode = (tmp_path / "token").stat().st_mode & 0o777
        assert mode == 0o600

    def test_load_missing(self, tmp_path: Path) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            assert load_token(key_dir=tmp_path) is None

    def test_clear_existing(self, tmp_path: Path) -> None:
        save_token(generate_token(), key_dir=tmp_path)
        assert clear_token(key_dir=tmp_path) is True
        with mock.patch.dict(os.environ, {}, clear=True):
            assert load_token(key_dir=tmp_path) is None


class TestEnvVar:
    def test_env_var_priority(self, tmp_path: Path) -> None:
        file_token = generate_token()
        env_token = generate_token()
        save_token(file_token, key_dir=tmp_path)
        with mock.patch.dict(os.environ, {_TOKEN_ENV_VAR: env_token}):
            assert load_token(key_dir=tmp_path) == env_token

    def test_invalid_env_var(self, tmp_path: Path) -> None:
        with mock.patch.dict(os.environ, {_TOKEN_ENV_VAR: "not-hex"}):
            assert load_token(key_dir=tmp_path) is None


class TestResolveRootToken:
    def test_from_env(self, tmp_path: Path) -> None:
        token = generate_token()
        with mock.patch.dict(os.environ, {_TOKEN_ENV_VAR: token}):
            raw = resolve_root_token("client", key_dir=tmp_path)
        assert raw == bytes.fromhex(token)

    def test_from_file(self, tmp_path: Path) -> None:
        token = generate_token()
        save_token(token, key_dir=tmp_path)
        with mock.patch.dict(os.environ, {}, clear=True):
            raw = resolve_root_token("worker", key_dir=tmp_path)
        assert raw == bytes.fromhex(token)

    def test_from_pairing_key(self, tmp_path: Path) -> None:
        from offwork.core.pairing import save_shared_key

        raw_key = b"\x42" * 32
        save_shared_key(raw_key, "client", key_dir=tmp_path)
        with mock.patch.dict(os.environ, {}, clear=True):
            assert resolve_root_token("client", key_dir=tmp_path) == raw_key

    def test_token_over_pairing(self, tmp_path: Path) -> None:
        from offwork.core.pairing import save_shared_key

        save_shared_key(b"\x99" * 32, "worker", key_dir=tmp_path)
        token = generate_token()
        save_token(token, key_dir=tmp_path)
        with mock.patch.dict(os.environ, {}, clear=True):
            assert resolve_root_token("worker", key_dir=tmp_path) == bytes.fromhex(token)

    def test_no_key_material(self, tmp_path: Path) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            assert resolve_root_token("client", key_dir=tmp_path) is None
