"""Pre-shared token used as the enrollment credential.

A token is a random 32-byte secret that both client and worker hold.
It is used to derive a per-client HMAC subkey at envelope-signing time
(see :mod:`offwork.core.envelope`).  By itself, the token cannot
impersonate an already-enrolled client — clients are additionally
pinned by their Ed25519 public key on first contact (TOFU).

Resolution order (highest priority first):

1. ``OFFWORK_SIGNING_TOKEN`` environment variable (hex-encoded)
2. ``~/.offwork/token`` file (hex-encoded)
3. ``~/.offwork/{client,worker}.key`` file (raw bytes, from pairing)

All primitives are stdlib-only.
"""

import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Environment variable for token distribution
_TOKEN_ENV_VAR = "OFFWORK_SIGNING_TOKEN"

# File persistence
_DEFAULT_KEY_DIR = Path.home() / ".offwork"
_TOKEN_FILE = "token"

# Token size in bytes
_TOKEN_BYTES = 32


def generate_token() -> str:
    """Generate a random signing token and return it as a hex string.

    The token is 32 bytes of cryptographically secure random data,
    encoded as a 64-character hexadecimal string.
    """
    return os.urandom(_TOKEN_BYTES).hex()


def save_token(
    token_hex: str,
    key_dir: Path | None = None,
) -> Path:
    """Persist a hex-encoded token to ``~/.offwork/token``.

    Parameters
    ----------
    token_hex
        The 64-character hex-encoded token string.
    key_dir
        Override the default ``~/.offwork`` directory.

    Returns
    -------
    Path
        The file that was written.

    Raises
    ------
    ValueError
        If *token_hex* is not a valid 64-character hex string.
    """
    _validate_token_hex(token_hex)
    d = _ensure_key_dir(key_dir)
    path = d / _TOKEN_FILE
    path.write_text(token_hex + "\n")
    path.chmod(0o600)
    logger.info("Saved token to %s", path)
    return path


def load_token(key_dir: Path | None = None) -> str | None:
    """Load a hex-encoded token from the environment or disk.

    Resolution order:

    1. ``OFFWORK_SIGNING_TOKEN`` environment variable
    2. ``~/.offwork/token`` file

    Returns ``None`` when no token is found.
    """
    # 1. Environment variable
    env_val = os.environ.get(_TOKEN_ENV_VAR)
    if env_val is not None:
        env_val = env_val.strip()
        if _is_valid_token_hex(env_val):
            return env_val
        logger.warning(
            "%s is set but contains an invalid token "
            "(expected 64 hex characters)",
            _TOKEN_ENV_VAR,
        )
        return None

    # 2. File on disk
    d = _ensure_key_dir(key_dir)
    path = d / _TOKEN_FILE
    if not path.exists():
        return None
    content = path.read_text().strip()
    if _is_valid_token_hex(content):
        return content
    logger.warning("Invalid token file %s (expected 64 hex characters)", path)
    return None


def clear_token(key_dir: Path | None = None) -> bool:
    """Delete a saved token file.  Returns ``True`` if a file was removed."""
    d = _ensure_key_dir(key_dir)
    path = d / _TOKEN_FILE
    if path.exists():
        path.unlink()
        logger.info("Removed token %s", path)
        return True
    return False


def resolve_root_token(role: str, key_dir: Path | None = None) -> bytes | None:
    """Resolve the **raw** shared secret used as the enrollment credential.

    Returns the raw 32 bytes (without any HKDF expansion) so callers can
    derive multiple subkeys from it — typically one per client_id.

    Precedence:

    1. ``OFFWORK_SIGNING_TOKEN`` environment variable
    2. ``~/.offwork/token`` file
    3. ``~/.offwork/{role}.key`` (from pairing)
    """
    from offwork.core.pairing import load_shared_key

    token_hex = load_token(key_dir)
    if token_hex is not None:
        return bytes.fromhex(token_hex)

    raw_key = load_shared_key(role, key_dir)
    if raw_key is not None:
        return raw_key

    return None





# -- Helpers ----------------------------------------------------------------


def _ensure_key_dir(key_dir: Path | None = None) -> Path:
    """Return the key directory, creating it if necessary."""
    d = key_dir or _DEFAULT_KEY_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _is_valid_token_hex(s: str) -> bool:
    """Return ``True`` if *s* is a valid 64-character hex string."""
    if len(s) != _TOKEN_BYTES * 2:
        return False
    try:
        bytes.fromhex(s)
    except ValueError:
        return False
    return True


def _validate_token_hex(token_hex: str) -> None:
    """Raise ``ValueError`` if *token_hex* is invalid."""
    if not _is_valid_token_hex(token_hex):
        raise ValueError(
            f"Invalid token: expected a 64-character hex string, "
            f"got {len(token_hex)} characters"
        )
