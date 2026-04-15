"""Cryptographic signing for pyfuse task authentication.

Provides Ed25519 key pairs for signing serialized task payloads so that
workers can verify the origin of submitted code.  The ``cryptography``
package is required (install with ``pip install pyfuse[signing]``).

Typical usage – **client side**::

    from pyfuse.core.signing import KeyPair

    kp = KeyPair.generate()
    kp.save("client.pem")                       # keep private
    kp.save_public("client.pub")                 # share with workers

Typical usage – **worker side**::

    from pyfuse.core.signing import TrustStore

    trust = TrustStore.from_directory("trusted_keys/")
    # pass *trust* to Worker or serve()
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Self


def _require_cryptography() -> None:
    """Raise a helpful error if the ``cryptography`` package is missing."""
    try:
        import cryptography  # noqa: F401
    except ModuleNotFoundError:
        raise ModuleNotFoundError(
            "The 'cryptography' package is required for task signing. "
            "Install it with:  pip install pyfuse[signing]"
        ) from None


# ---------------------------------------------------------------------------
# KeyPair – client signing identity
# ---------------------------------------------------------------------------


class KeyPair:
    """Ed25519 signing key pair for a pyfuse client.

    A ``KeyPair`` holds both the private and public key.  Use
    :meth:`generate` to create a new identity, or :meth:`from_file` /
    :meth:`from_private_bytes` to reload one.
    """

    def __init__(
        self,
        private_key: "Ed25519PrivateKey",  # noqa: F821 – lazy import
    ) -> None:
        _require_cryptography()
        self._private_key = private_key
        self._public_key = private_key.public_key()

    # -- factories ---------------------------------------------------------

    @classmethod
    def generate(cls) -> Self:
        """Generate a fresh Ed25519 key pair."""
        _require_cryptography()
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_private_bytes(cls, data: bytes) -> Self:
        """Load from 32-byte raw private key seed."""
        _require_cryptography()
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        return cls(Ed25519PrivateKey.from_private_bytes(data))

    @classmethod
    def from_file(cls, path: str | Path) -> Self:
        """Load a private key from a PEM file."""
        _require_cryptography()
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        raw = Path(path).read_bytes()
        key = load_pem_private_key(raw, password=None)
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        if not isinstance(key, Ed25519PrivateKey):
            raise TypeError(
                f"Expected an Ed25519 private key, got {type(key).__name__}"
            )
        return cls(key)

    # -- persistence -------------------------------------------------------

    def save(self, path: str | Path) -> None:
        """Write the private key to *path* in PEM format (mode 0600)."""
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
        )

        pem = self._private_key.private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption(),
        )
        p = Path(path)
        p.write_bytes(pem)
        os.chmod(p, 0o600)

    def save_public(self, path: str | Path) -> None:
        """Write the public key to *path* in PEM format."""
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            PublicFormat,
        )

        pem = self._public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        Path(path).write_bytes(pem)

    # -- identity ----------------------------------------------------------

    @property
    def public_bytes(self) -> bytes:
        """Raw 32-byte Ed25519 public key."""
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            PublicFormat,
        )

        return self._public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)

    @property
    def fingerprint(self) -> str:
        """SHA-256 hex digest of the raw public key bytes."""
        return _fingerprint(self.public_bytes)

    # -- crypto operations -------------------------------------------------

    def sign(self, data: bytes) -> bytes:
        """Sign *data* and return the 64-byte Ed25519 signature."""
        return self._private_key.sign(data)


# ---------------------------------------------------------------------------
# TrustStore – set of trusted public keys (worker side)
# ---------------------------------------------------------------------------


class TrustStore:
    """A collection of trusted client public keys.

    The worker uses a ``TrustStore`` to decide which clients are allowed
    to submit tasks.  Public keys can be loaded from ``.pub`` PEM files
    in a directory, or added programmatically.

    Example::

        trust = TrustStore.from_directory("/etc/pyfuse/trusted_keys")
        assert trust.is_trusted(some_fingerprint)
    """

    def __init__(self) -> None:
        _require_cryptography()
        self._keys: dict[str, bytes] = {}  # fingerprint -> raw public bytes

    # -- factories ---------------------------------------------------------

    @classmethod
    def from_directory(cls, path: str | Path) -> Self:
        """Load every ``.pub`` PEM file from *path*.

        Each file should contain a single Ed25519 public key in PEM format
        (as produced by :meth:`KeyPair.save_public`).
        """
        store = cls()
        d = Path(path)
        if not d.is_dir():
            raise NotADirectoryError(f"Trusted keys directory not found: {d}")
        for f in sorted(d.iterdir()):
            if f.suffix == ".pub" and f.is_file():
                store.add_public_key_file(f)
        return store

    @classmethod
    def from_fingerprints(cls, fingerprints: set[str]) -> Self:
        """Create a trust store that trusts the given fingerprints.

        This variant does not hold actual public key bytes — it can only
        check whether a fingerprint is trusted, but cannot perform
        signature verification itself.  Use :meth:`verify_task` on
        :class:`~pyfuse.core.task.Task` instead.
        """
        store = cls()
        for fp in fingerprints:
            store._keys[fp] = b""  # placeholder – fingerprint-only trust
        return store

    # -- mutators ----------------------------------------------------------

    def add_public_key_file(self, path: str | Path) -> str:
        """Load a PEM public key file and add it to the store.

        Returns the fingerprint of the added key.
        """
        raw_bytes = _load_public_key_bytes(Path(path))
        fp = _fingerprint(raw_bytes)
        self._keys[fp] = raw_bytes
        return fp

    def add_public_bytes(self, raw: bytes) -> str:
        """Add a raw 32-byte Ed25519 public key.

        Returns the fingerprint.
        """
        fp = _fingerprint(raw)
        self._keys[fp] = raw
        return fp

    # -- queries -----------------------------------------------------------

    def is_trusted(self, fingerprint: str) -> bool:
        """Return ``True`` if *fingerprint* is in the trust store."""
        return fingerprint in self._keys

    @property
    def fingerprints(self) -> frozenset[str]:
        """All trusted fingerprints."""
        return frozenset(self._keys)

    def __len__(self) -> int:
        return len(self._keys)

    def __bool__(self) -> bool:
        return len(self._keys) > 0

    # -- verification ------------------------------------------------------

    def verify(self, data: bytes, signature: bytes, signer_public_bytes: bytes) -> bool:
        """Verify *signature* over *data* against a trusted public key.

        Returns ``True`` only if the signer's key is trusted **and** the
        signature is valid.
        """
        fp = _fingerprint(signer_public_bytes)
        if fp not in self._keys:
            return False
        return _verify(signer_public_bytes, data, signature)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _fingerprint(raw_public_bytes: bytes) -> str:
    """SHA-256 hex digest of raw Ed25519 public key bytes."""
    return hashlib.sha256(raw_public_bytes).hexdigest()


def _verify(public_bytes: bytes, data: bytes, signature: bytes) -> bool:
    """Verify an Ed25519 signature.  Returns ``False`` on failure."""
    _require_cryptography()
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        key = Ed25519PublicKey.from_public_bytes(public_bytes)
        key.verify(signature, data)
        return True
    except (InvalidSignature, ValueError):
        return False


def _load_public_key_bytes(path: Path) -> bytes:
    """Read a PEM public key file and return raw 32-byte Ed25519 public key."""
    _require_cryptography()
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
        load_pem_public_key,
    )

    pem = path.read_bytes()
    key = load_pem_public_key(pem)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if not isinstance(key, Ed25519PublicKey):
        raise TypeError(
            f"Expected an Ed25519 public key in {path}, got {type(key).__name__}"
        )
    return key.public_bytes(Encoding.Raw, PublicFormat.Raw)
