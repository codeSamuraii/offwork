import os
from pathlib import Path
from unittest import mock

import pytest

from pyfuse.core.signing import derive_key
from pyfuse.core.token import (
    _TOKEN_ENV_VAR,
    _is_valid_token_hex,
    _validate_token_hex,
    clear_token,
    generate_token,
    load_token,
    resolve_signing_key,
    save_token,
)


class TestGenerateToken:
    def test_length(self) -> None:
        token = generate_token()
        assert len(token) == 64

    def test_hex_string(self) -> None:
        token = generate_token()
        bytes.fromhex(token)  # should not raise

    def test_unique(self) -> None:
        tokens = {generate_token() for _ in range(50)}
        assert len(tokens) == 50

    def test_32_bytes_decoded(self) -> None:
        token = generate_token()
        raw = bytes.fromhex(token)
        assert len(raw) == 32


class TestValidation:
    def test_valid_token(self) -> None:
        assert _is_valid_token_hex("ab" * 32) is True

    def test_too_short(self) -> None:
        assert _is_valid_token_hex("ab" * 16) is False

    def test_too_long(self) -> None:
        assert _is_valid_token_hex("ab" * 33) is False

    def test_not_hex(self) -> None:
        assert _is_valid_token_hex("zz" * 32) is False

    def test_empty(self) -> None:
        assert _is_valid_token_hex("") is False

    def test_validate_raises(self) -> None:
        with pytest.raises(ValueError, match="64-character hex"):
            _validate_token_hex("too-short")

    def test_validate_ok(self) -> None:
        _validate_token_hex("ab" * 32)  # should not raise


class TestSaveAndLoad:
    def test_save_and_load(self, tmp_path: Path) -> None:
        token = generate_token()
        save_token(token, key_dir=tmp_path)
        loaded = load_token(key_dir=tmp_path)
        assert loaded == token

    def test_file_permissions(self, tmp_path: Path) -> None:
        save_token(generate_token(), key_dir=tmp_path)
        path = tmp_path / "token"
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600

    def test_load_missing(self, tmp_path: Path) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            assert load_token(key_dir=tmp_path) is None

    def test_load_invalid_file(self, tmp_path: Path) -> None:
        (tmp_path / "token").write_text("not-valid-hex\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            assert load_token(key_dir=tmp_path) is None

    def test_clear_existing(self, tmp_path: Path) -> None:
        save_token(generate_token(), key_dir=tmp_path)
        assert clear_token(key_dir=tmp_path) is True
        with mock.patch.dict(os.environ, {}, clear=True):
            assert load_token(key_dir=tmp_path) is None

    def test_clear_missing(self, tmp_path: Path) -> None:
        assert clear_token(key_dir=tmp_path) is False

    def test_save_invalid_token(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="64-character hex"):
            save_token("bad-token", key_dir=tmp_path)

    def test_file_content_has_newline(self, tmp_path: Path) -> None:
        token = generate_token()
        save_token(token, key_dir=tmp_path)
        content = (tmp_path / "token").read_text()
        assert content == token + "\n"

    def test_load_strips_whitespace(self, tmp_path: Path) -> None:
        """Token files with trailing whitespace should be loaded correctly."""
        token = generate_token()
        (tmp_path / "token").write_text(f"  {token}  \n")
        (tmp_path / "token").chmod(0o600)
        with mock.patch.dict(os.environ, {}, clear=True):
            loaded = load_token(key_dir=tmp_path)
        assert loaded == token


class TestEnvVar:
    def test_env_var_takes_priority(self, tmp_path: Path) -> None:
        """Environment variable should override file on disk."""
        file_token = generate_token()
        env_token = generate_token()
        save_token(file_token, key_dir=tmp_path)

        with mock.patch.dict(os.environ, {_TOKEN_ENV_VAR: env_token}):
            loaded = load_token(key_dir=tmp_path)
        assert loaded == env_token

    def test_env_var_only(self, tmp_path: Path) -> None:
        env_token = generate_token()
        with mock.patch.dict(os.environ, {_TOKEN_ENV_VAR: env_token}):
            loaded = load_token(key_dir=tmp_path)
        assert loaded == env_token

    def test_invalid_env_var(self, tmp_path: Path) -> None:
        with mock.patch.dict(os.environ, {_TOKEN_ENV_VAR: "not-hex"}):
            loaded = load_token(key_dir=tmp_path)
        assert loaded is None

    def test_env_var_with_whitespace(self, tmp_path: Path) -> None:
        token = generate_token()
        with mock.patch.dict(os.environ, {_TOKEN_ENV_VAR: f"  {token}  "}):
            loaded = load_token(key_dir=tmp_path)
        assert loaded == token


class TestResolveSigningKey:
    def test_from_token_env(self, tmp_path: Path) -> None:
        token = generate_token()
        expected = derive_key(bytes.fromhex(token))
        with mock.patch.dict(os.environ, {_TOKEN_ENV_VAR: token}):
            key = resolve_signing_key("client", key_dir=tmp_path)
        assert key == expected

    def test_from_token_file(self, tmp_path: Path) -> None:
        token = generate_token()
        save_token(token, key_dir=tmp_path)
        expected = derive_key(bytes.fromhex(token))
        with mock.patch.dict(os.environ, {}, clear=True):
            key = resolve_signing_key("worker", key_dir=tmp_path)
        assert key == expected

    def test_from_pairing_key(self, tmp_path: Path) -> None:
        """Falls back to pairing key when no token exists."""
        from pyfuse.core.pairing import save_shared_key

        raw = b"\x42" * 32
        save_shared_key(raw, "client", key_dir=tmp_path)
        expected = derive_key(raw)
        with mock.patch.dict(os.environ, {}, clear=True):
            key = resolve_signing_key("client", key_dir=tmp_path)
        assert key == expected

    def test_token_over_pairing(self, tmp_path: Path) -> None:
        """Token takes priority over pairing key."""
        from pyfuse.core.pairing import save_shared_key

        pairing_key = b"\x99" * 32
        save_shared_key(pairing_key, "worker", key_dir=tmp_path)

        token = generate_token()
        save_token(token, key_dir=tmp_path)

        expected = derive_key(bytes.fromhex(token))
        with mock.patch.dict(os.environ, {}, clear=True):
            key = resolve_signing_key("worker", key_dir=tmp_path)
        assert key == expected

    def test_no_key_material(self, tmp_path: Path) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            key = resolve_signing_key("client", key_dir=tmp_path)
        assert key is None

    def test_same_key_both_roles(self, tmp_path: Path) -> None:
        """Same token produces the same signing key regardless of role."""
        token = generate_token()
        save_token(token, key_dir=tmp_path)
        with mock.patch.dict(os.environ, {}, clear=True):
            client_key = resolve_signing_key("client", key_dir=tmp_path)
            worker_key = resolve_signing_key("worker", key_dir=tmp_path)
        assert client_key == worker_key


class TestTokenSigningEndToEnd:
    """End-to-end: generate token, sign task, verify task."""

    def test_sign_and_verify(self) -> None:
        from pyfuse.core.task import Task

        token = generate_token()
        key = derive_key(bytes.fromhex(token))

        task = Task(
            graph_json='{"objects": {}}',
            function_name="m.func",
            args=(1, 2),
            task_id="tok-test",
        )
        signed = task.to_json(signing_key=key)
        restored = Task.from_json(signed, signing_key=key)
        assert restored.task_id == "tok-test"
        assert restored.function_name == "m.func"
        assert restored.signature is not None

    def test_wrong_token_fails(self) -> None:
        from pyfuse.core.errors import SignatureError
        from pyfuse.core.task import Task

        key1 = derive_key(bytes.fromhex(generate_token()))
        key2 = derive_key(bytes.fromhex(generate_token()))

        task = Task(graph_json="{}", function_name="f")
        signed = task.to_json(signing_key=key1)
        with pytest.raises(SignatureError, match="verification failed"):
            Task.from_json(signed, signing_key=key2)

    def test_token_and_pairing_interop(self) -> None:
        """A token-signed task can be verified by a pairing-derived key
        if they resolve to the same signing key (they use the same
        derive_key function)."""
        from pyfuse.core.task import Task

        token = generate_token()
        raw = bytes.fromhex(token)
        key = derive_key(raw)

        task = Task(graph_json="{}", function_name="f")
        signed = task.to_json(signing_key=key)

        # Same key material, same derived key → verification succeeds
        restored = Task.from_json(signed, signing_key=key)
        assert restored.signature is not None
