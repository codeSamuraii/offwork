"""Per-machine cryptographic identity.

On first use, each client (and worker) auto-generates:

- ``~/.offwork/client_id`` — a stable 16-byte random identifier, encoded
  as 32 hex characters.  Lets the worker tell distinct clients apart
  without any per-deployment configuration.
- ``~/.offwork/identity.key`` — a 32-byte Ed25519 seed.  The matching
  public key travels with every signed task envelope and is pinned
  TOFU-style by the worker on first contact.

Both files are persisted with ``0o600`` permissions.  Users never
interact with them directly — they exist so the worker can pin a
client's identity independently of the shared signing token.
"""

import os
import hashlib
import logging
from pathlib import Path

from offwork.core import ed25519

logger = logging.getLogger(__name__)

_DEFAULT_KEY_DIR = Path.home() / ".offwork"
_CLIENT_ID_FILE = "client_id"
_IDENTITY_FILE = "identity.key"
_CLIENT_ID_BYTES = 16


def _ensure_dir(key_dir: Path | None) -> Path:
    d = key_dir or _DEFAULT_KEY_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _is_valid_client_id(s: str) -> bool:
    if len(s) != _CLIENT_ID_BYTES * 2:
        return False
    try:
        bytes.fromhex(s)
    except ValueError:
        return False
    return True


def get_client_id(key_dir: Path | None = None) -> str:
    """Return this machine's stable client_id, auto-generating it once.

    The id is stored as 32 hex characters in ``~/.offwork/client_id``.
    """
    d = _ensure_dir(key_dir)
    path = d / _CLIENT_ID_FILE
    if path.exists():
        content = path.read_text().strip()
        if _is_valid_client_id(content):
            return content
        logger.warning("Invalid client_id at %s — regenerating", path)
    cid = os.urandom(_CLIENT_ID_BYTES).hex()
    path.write_text(cid + "\n")
    path.chmod(0o600)
    logger.info("Generated new client_id %s", cid[:8])
    return cid


def get_identity_seed(key_dir: Path | None = None) -> bytes:
    """Return this machine's Ed25519 seed, auto-generating it once.

    The seed is stored raw (32 bytes) in ``~/.offwork/identity.key``.
    """
    d = _ensure_dir(key_dir)
    path = d / _IDENTITY_FILE
    if path.exists():
        data = path.read_bytes()
        if len(data) == 32:
            return data
        logger.warning("Invalid identity seed at %s — regenerating", path)
    seed = ed25519.generate_seed()
    path.write_bytes(seed)
    path.chmod(0o600)
    logger.info("Generated new identity keypair")
    return seed


def get_public_key(key_dir: Path | None = None) -> bytes:
    """Return this machine's 32-byte Ed25519 public key."""
    return ed25519.seed_to_public(get_identity_seed(key_dir))


def get_identity_fingerprint(key_dir: Path | None = None) -> str:
    """Return a short, human-friendly fingerprint of the public key."""
    pub = get_public_key(key_dir)
    return hashlib.sha256(pub).hexdigest()[:16]


def clear_identity(key_dir: Path | None = None) -> bool:
    """Delete ``client_id`` and ``identity.key`` files.

    Returns ``True`` if at least one file was removed.
    """
    d = _ensure_dir(key_dir)
    removed = False
    for name in (_CLIENT_ID_FILE, _IDENTITY_FILE):
        p = d / name
        if p.exists():
            p.unlink()
            removed = True
    if removed:
        logger.info("Cleared local identity files")
    return removed
