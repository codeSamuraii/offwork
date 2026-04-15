"""SPAKE2-based automated pairing for pyfuse client-worker trust.

Bootstraps trust between a client and worker using a short shared pairing
code.  The protocol derives a session key via SPAKE2 (Password-Authenticated
Key Exchange), then uses it to encrypt the client's Ed25519 public key for
delivery to the worker's trust store.

Requires ``pip install pyfuse[pairing]``.

Worker side::

    transport = RedisPairingTransport("redis://localhost:6379")
    result = await accept_pairing(transport, code="847291",
                                  trusted_keys_dir="/etc/pyfuse/keys")

Client side::

    transport = RedisPairingTransport("redis://localhost:6379")
    result = await request_pairing(transport, code="847291",
                                   save_path="~/.pyfuse/key.pem")
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pyfuse.core.signing import KeyPair


def _require_pairing_deps() -> None:
    """Raise a helpful error if pairing dependencies are missing."""
    try:
        import spake2  # noqa: F401
    except ModuleNotFoundError:
        raise ModuleNotFoundError(
            "The 'spake2' package is required for automated pairing. "
            "Install it with:  pip install pyfuse[pairing]"
        ) from None
    try:
        import cryptography  # noqa: F401
    except ModuleNotFoundError:
        raise ModuleNotFoundError(
            "The 'cryptography' package is required for automated pairing. "
            "Install it with:  pip install pyfuse[pairing]"
        ) from None


# ---------------------------------------------------------------------------
# Transport protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class PairingTransport(Protocol):
    """Async key-value transport for pairing message exchange.

    Implementations must support concurrent readers and writers (the
    worker and client run the protocol simultaneously).
    """

    async def put(self, key: str, value: bytes) -> None:
        """Store *value* under *key*."""
        ...

    async def get(self, key: str, timeout: float = 60.0) -> bytes | None:
        """Poll for a value under *key*.  Returns ``None`` on timeout."""
        ...


# ---------------------------------------------------------------------------
# Pairing code helpers
# ---------------------------------------------------------------------------


def generate_pairing_code(length: int = 6) -> str:
    """Generate a cryptographically random numeric pairing code."""
    return "".join(str(secrets.randbelow(10)) for _ in range(length))


def _channel_prefix(code: str) -> str:
    """Derive a channel prefix from a pairing code.

    Uses a salted hash so observers cannot identify which code is in use.
    """
    return "pyfuse:pair:" + hashlib.sha256(
        b"pyfuse-pairing-v1:" + code.encode()
    ).hexdigest()[:16]


# ---------------------------------------------------------------------------
# AES-256-GCM encryption helpers
# ---------------------------------------------------------------------------


def _derive_aes_key(session_key: bytes) -> bytes:
    """Derive a 256-bit AES key from the SPAKE2 session key."""
    return hashlib.sha256(b"pyfuse-aes:" + session_key).digest()


def _encrypt(session_key: bytes, plaintext: bytes) -> bytes:
    """Encrypt *plaintext* with AES-256-GCM.  Returns ``nonce || ciphertext``."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    aes_key = _derive_aes_key(session_key)
    nonce = os.urandom(12)
    aesgcm = AESGCM(aes_key)
    ct = aesgcm.encrypt(nonce, plaintext, None)
    return nonce + ct


def _decrypt(session_key: bytes, data: bytes) -> bytes:
    """Decrypt AES-256-GCM *data* (``nonce || ciphertext``)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    aes_key = _derive_aes_key(session_key)
    nonce = data[:12]
    ct = data[12:]
    aesgcm = AESGCM(aes_key)
    return aesgcm.decrypt(nonce, ct, None)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PairingResult:
    """Outcome of a successful pairing."""

    fingerprint: str
    """SHA-256 hex fingerprint of the enrolled Ed25519 public key."""

    public_key_bytes: bytes
    """Raw 32-byte Ed25519 public key."""


# ---------------------------------------------------------------------------
# Worker side — accept pairing
# ---------------------------------------------------------------------------


async def accept_pairing(
    transport: PairingTransport,
    code: str,
    *,
    trusted_keys_dir: str | Path | None = None,
    timeout: float = 60.0,
) -> PairingResult:
    """Worker side: accept a client pairing request.

    Runs the SPAKE2 protocol as side B, receives the client's encrypted
    Ed25519 public key, decrypts it, and optionally persists it as a
    ``.pub`` file in *trusted_keys_dir*.

    Parameters
    ----------
    transport
        Async key-value transport for message exchange.
    code
        Shared pairing code (e.g. 6-digit PIN).
    trusted_keys_dir
        Directory to save the client's ``.pub`` key file.
    timeout
        Seconds to wait for the client (default: 60).

    Raises
    ------
    TimeoutError
        If the client does not respond within *timeout*.
    ValueError
        If decryption fails (wrong pairing code or corrupted data).
    """
    _require_pairing_deps()
    from spake2 import SPAKE2_B  # type: ignore[import-untyped]

    prefix = _channel_prefix(code)

    # Step 1: Generate and publish SPAKE2_B message
    sp = SPAKE2_B(code.encode())
    msg_b = sp.start()
    await transport.put(f"{prefix}:b", msg_b)

    # Step 2: Wait for client's SPAKE2_A message
    msg_a = await transport.get(f"{prefix}:a", timeout=timeout)
    if msg_a is None:
        raise TimeoutError("Pairing timed out waiting for client.")

    # Step 3: Derive session key
    session_key = sp.finish(msg_a)

    # Step 4: Wait for encrypted public key
    encrypted_key = await transport.get(f"{prefix}:key", timeout=timeout)
    if encrypted_key is None:
        raise TimeoutError("Pairing timed out waiting for client key.")

    # Step 5: Decrypt public key
    try:
        public_key_bytes = _decrypt(session_key, encrypted_key)
    except Exception as exc:
        raise ValueError(
            "Failed to decrypt client key — wrong pairing code or corrupted data."
        ) from exc

    if len(public_key_bytes) != 32:
        raise ValueError(
            f"Invalid public key: expected 32 bytes, got {len(public_key_bytes)}"
        )

    # Step 6: Compute fingerprint
    from pyfuse.core.signing import _fingerprint

    fp = _fingerprint(public_key_bytes)

    # Step 7: Persist if requested
    if trusted_keys_dir is not None:
        _save_public_key(public_key_bytes, fp, Path(trusted_keys_dir))

    # Step 8: Send acknowledgment
    await transport.put(f"{prefix}:ack", fp.encode())

    return PairingResult(fingerprint=fp, public_key_bytes=public_key_bytes)


# ---------------------------------------------------------------------------
# Client side — request pairing
# ---------------------------------------------------------------------------


async def request_pairing(
    transport: PairingTransport,
    code: str,
    *,
    keypair: "KeyPair | None" = None,
    save_path: str | Path | None = None,
    timeout: float = 60.0,
) -> PairingResult:
    """Client side: pair with a worker.

    Runs the SPAKE2 protocol as side A, encrypts the client's Ed25519
    public key with the derived session key, and sends it to the worker.

    Parameters
    ----------
    transport
        Async key-value transport for message exchange.
    code
        Shared pairing code (must match the worker's code).
    keypair
        Existing :class:`~pyfuse.core.signing.KeyPair`.  A new one is
        generated when *None*.
    save_path
        Path to save the generated keypair (``*.pem`` + ``*.pub``).
        Only used when *keypair* is None.
    timeout
        Seconds to wait for the worker (default: 60).

    Raises
    ------
    TimeoutError
        If the worker does not respond within *timeout*.
    ValueError
        If the pairing code doesn't match (acknowledgment mismatch).
    """
    _require_pairing_deps()
    from spake2 import SPAKE2_A  # type: ignore[import-untyped]

    from pyfuse.core.signing import KeyPair

    prefix = _channel_prefix(code)

    # Generate or reuse keypair
    if keypair is None:
        keypair = KeyPair.generate()
        if save_path is not None:
            p = Path(save_path)
            keypair.save(p)
            keypair.save_public(p.with_suffix(".pub"))

    # Step 1: Generate and publish SPAKE2_A message
    sp = SPAKE2_A(code.encode())
    msg_a = sp.start()
    await transport.put(f"{prefix}:a", msg_a)

    # Step 2: Wait for worker's SPAKE2_B message
    msg_b = await transport.get(f"{prefix}:b", timeout=timeout)
    if msg_b is None:
        raise TimeoutError("Pairing timed out waiting for worker.")

    # Step 3: Derive session key
    session_key = sp.finish(msg_b)

    # Step 4: Encrypt and send public key
    encrypted_key = _encrypt(session_key, keypair.public_bytes)
    await transport.put(f"{prefix}:key", encrypted_key)

    # Step 5: Wait for acknowledgment
    ack = await transport.get(f"{prefix}:ack", timeout=timeout)
    if ack is None:
        raise TimeoutError("Pairing timed out waiting for worker confirmation.")

    received_fp = ack.decode()
    expected_fp = keypair.fingerprint
    if received_fp != expected_fp:
        raise ValueError(
            f"Fingerprint mismatch: expected {expected_fp}, got {received_fp}"
        )

    return PairingResult(fingerprint=expected_fp, public_key_bytes=keypair.public_bytes)


# ---------------------------------------------------------------------------
# Public key persistence
# ---------------------------------------------------------------------------


def _save_public_key(raw_bytes: bytes, fingerprint: str, directory: Path) -> Path:
    """Write a raw Ed25519 public key as a PEM ``.pub`` file."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key = Ed25519PublicKey.from_public_bytes(raw_bytes)
    pem = key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{fingerprint[:16]}.pub"
    path.write_bytes(pem)
    return path


# ---------------------------------------------------------------------------
# In-memory transport (for testing)
# ---------------------------------------------------------------------------


class MemoryPairingTransport:
    """In-memory :class:`PairingTransport` backed by an :class:`asyncio.Event`-based store.

    Suitable for unit tests where both sides run as concurrent tasks
    in the same event loop.
    """

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}
        self._events: dict[str, asyncio.Event] = {}

    def _event(self, key: str) -> asyncio.Event:
        if key not in self._events:
            self._events[key] = asyncio.Event()
        return self._events[key]

    async def put(self, key: str, value: bytes) -> None:
        self._store[key] = value
        self._event(key).set()

    async def get(self, key: str, timeout: float = 60.0) -> bytes | None:
        event = self._event(key)
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return self._store.get(key)


# ---------------------------------------------------------------------------
# Redis transport
# ---------------------------------------------------------------------------


class RedisPairingTransport:
    """Redis-backed :class:`PairingTransport`.

    Keys are set with a short TTL (2 minutes) so pairing artifacts
    are automatically cleaned up.
    """

    PAIRING_TTL = 120  # seconds

    def __init__(self, url: str) -> None:
        try:
            import redis.asyncio as _redis
        except ModuleNotFoundError:
            raise ModuleNotFoundError(
                "The 'redis' package is required for Redis pairing transport. "
                "Install it with:  pip install pyfuse[redis]"
            ) from None
        self._redis = _redis.Redis.from_url(url)

    async def put(self, key: str, value: bytes) -> None:
        await self._redis.set(key, value, ex=self.PAIRING_TTL)

    async def get(self, key: str, timeout: float = 60.0) -> bytes | None:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            raw = await self._redis.get(key)
            if raw is not None:
                return raw if isinstance(raw, bytes) else raw.encode()
            await asyncio.sleep(0.1)
        return None

    async def close(self) -> None:
        """Close the underlying Redis connection."""
        await self._redis.aclose()
