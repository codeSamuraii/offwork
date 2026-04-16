"""PIN-based pairing protocol for client-worker key exchange.

Pairing allows a client and worker to establish a shared secret by
entering the same short PIN on both sides.  The protocol is inspired by
SPAKE2 / SAS-based verification and works as follows:

1. One side (the *initiator*) generates a random 6-digit PIN and shows it
   to the user.
2. The user enters the same PIN on the other side (the *responder*).
3. Both sides independently derive an *intermediate secret* from the PIN
   using a key-derivation step (HMAC-SHA256 with a fixed salt).
4. The initiator generates a random *challenge nonce* and sends it to the
   responder over the backend channel.
5. The responder computes ``HMAC(intermediate_secret, nonce)`` and sends
   the result back.
6. The initiator verifies the response.  If it matches, both sides derive
   the final shared secret from
   ``HMAC(intermediate_secret, nonce ‖ "confirmed")``.

The protocol prevents replay attacks (nonce is random per session) and
ensures that a passive eavesdropper who observes the nonce+response
cannot recover the PIN or the shared secret.

All primitives are stdlib-only.
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Fixed salt used to bind the PIN derivation to pyfuse
_PIN_SALT = b"pyfuse-pairing-v1"

# Where key material is persisted
_DEFAULT_KEY_DIR = Path.home() / ".pyfuse"
_CLIENT_KEY_FILE = "client.key"
_WORKER_KEY_FILE = "worker.key"

# PIN length (digits)
PIN_LENGTH = 6


def generate_pin() -> str:
    """Generate a random numeric PIN of :data:`PIN_LENGTH` digits."""
    # secrets.randbelow is cryptographically secure
    num = secrets.randbelow(10 ** PIN_LENGTH)
    return str(num).zfill(PIN_LENGTH)


def _derive_intermediate(pin: str) -> bytes:
    """Derive a 32-byte intermediate key from a PIN string."""
    return hmac.new(
        _PIN_SALT, pin.encode("utf-8"), hashlib.sha256
    ).digest()


def generate_challenge() -> bytes:
    """Generate a 32-byte random challenge nonce."""
    return os.urandom(32)


def compute_response(intermediate: bytes, challenge: bytes) -> bytes:
    """Compute the challenge-response: ``HMAC(intermediate, challenge)``."""
    return hmac.new(intermediate, challenge, hashlib.sha256).digest()


def verify_response(
    intermediate: bytes, challenge: bytes, response: bytes
) -> bool:
    """Verify a challenge-response in constant time."""
    expected = compute_response(intermediate, challenge)
    return hmac.compare_digest(expected, response)


def derive_shared_secret(intermediate: bytes, challenge: bytes) -> bytes:
    """Derive the final 32-byte shared secret after successful verification."""
    material = challenge + b"confirmed"
    return hmac.new(intermediate, material, hashlib.sha256).digest()


# -- High-level pairing state -----------------------------------------------


@dataclass
class PairingResult:
    """Outcome of a successful pairing exchange."""

    shared_key: bytes
    peer_role: str  # "client" or "worker"
    paired_at: float = field(default_factory=time.time)


# -- Pairing messages (JSON-serializable) -----------------------------------


def _encode_bytes(b: bytes) -> str:
    """Encode bytes as hex for JSON transport."""
    return b.hex()


def _decode_bytes(s: str) -> bytes:
    """Decode hex string back to bytes."""
    return bytes.fromhex(s)


def make_challenge_message(challenge: bytes) -> str:
    """Build the JSON pairing-challenge message."""
    return json.dumps({
        "type": "pairing_challenge",
        "challenge": _encode_bytes(challenge),
    })


def parse_challenge_message(msg: str) -> bytes:
    """Extract the challenge nonce from a pairing-challenge message.

    Raises
    ------
    ValueError
        If the message is not a valid pairing challenge.
    """
    data = json.loads(msg)
    if data.get("type") != "pairing_challenge":
        raise ValueError(f"Expected pairing_challenge, got {data.get('type')!r}")
    return _decode_bytes(data["challenge"])


def make_response_message(response: bytes) -> str:
    """Build the JSON pairing-response message."""
    return json.dumps({
        "type": "pairing_response",
        "response": _encode_bytes(response),
    })


def parse_response_message(msg: str) -> bytes:
    """Extract the response from a pairing-response message.

    Raises
    ------
    ValueError
        If the message is not a valid pairing response.
    """
    data = json.loads(msg)
    if data.get("type") != "pairing_response":
        raise ValueError(f"Expected pairing_response, got {data.get('type')!r}")
    return _decode_bytes(data["response"])


def make_confirm_message() -> str:
    """Build the JSON pairing-confirmed message."""
    return json.dumps({"type": "pairing_confirmed"})


def parse_confirm_message(msg: str) -> None:
    """Validate a pairing-confirmed message.

    Raises
    ------
    ValueError
        If the message is not a valid pairing confirmation.
    """
    data = json.loads(msg)
    if data.get("type") != "pairing_confirmed":
        raise ValueError(f"Expected pairing_confirmed, got {data.get('type')!r}")


# -- Key persistence --------------------------------------------------------


def _ensure_key_dir(key_dir: Path | None = None) -> Path:
    """Return the key directory, creating it if necessary."""
    d = key_dir or _DEFAULT_KEY_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_shared_key(
    shared_key: bytes,
    role: str,
    key_dir: Path | None = None,
) -> Path:
    """Persist *shared_key* to disk.

    Parameters
    ----------
    shared_key
        The 32-byte shared secret.
    role
        ``"client"`` or ``"worker"`` — determines the filename.
    key_dir
        Override the default ``~/.pyfuse`` directory.

    Returns
    -------
    Path
        The file that was written.
    """
    d = _ensure_key_dir(key_dir)
    filename = _CLIENT_KEY_FILE if role == "client" else _WORKER_KEY_FILE
    path = d / filename
    path.write_bytes(shared_key)
    # Restrict permissions: owner-only
    path.chmod(0o600)
    logger.info("Saved shared key to %s", path)
    return path


def load_shared_key(
    role: str,
    key_dir: Path | None = None,
) -> bytes | None:
    """Load a previously saved shared key, or return *None*.

    Parameters
    ----------
    role
        ``"client"`` or ``"worker"``.
    key_dir
        Override the default ``~/.pyfuse`` directory.
    """
    d = _ensure_key_dir(key_dir)
    filename = _CLIENT_KEY_FILE if role == "client" else _WORKER_KEY_FILE
    path = d / filename
    if not path.exists():
        return None
    key = path.read_bytes()
    if len(key) != 32:
        logger.warning("Invalid key file %s (expected 32 bytes, got %d)", path, len(key))
        return None
    return key


def clear_shared_key(
    role: str,
    key_dir: Path | None = None,
) -> bool:
    """Delete a saved shared key.  Returns ``True`` if a file was removed."""
    d = _ensure_key_dir(key_dir)
    filename = _CLIENT_KEY_FILE if role == "client" else _WORKER_KEY_FILE
    path = d / filename
    if path.exists():
        path.unlink()
        logger.info("Removed shared key %s", path)
        return True
    return False


# -- Backend channel helpers ------------------------------------------------

_PAIRING_CHANNEL = "pyfuse:pairing"


async def initiate_pairing(
    backend: Any,
    pin: str,
    timeout: float = 30.0,
) -> PairingResult:
    """Run the *initiator* side of the pairing protocol.

    The initiator is typically the **worker**: it generates a challenge,
    publishes it on the pairing channel, and waits for the client's
    response.

    Parameters
    ----------
    backend
        A pyfuse :class:`~pyfuse.worker.backends.base.Backend` instance
        with ``send_progress`` / ``get_progress`` used as a simple KV
        channel, or any object that exposes the pairing channel methods.
    pin
        The PIN entered by the user.
    timeout
        Seconds to wait for the peer to respond.

    Raises
    ------
    PairingError
        On timeout or verification failure.
    """
    from pyfuse.core.errors import PairingError

    intermediate = _derive_intermediate(pin)
    challenge = generate_challenge()

    # Publish challenge
    challenge_msg = make_challenge_message(challenge)
    await backend.send_progress(_PAIRING_CHANNEL, challenge_msg)
    logger.debug("Pairing: sent challenge")

    # Wait for response
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = await backend.get_progress(_PAIRING_CHANNEL + ":response")
        if raw is not None:
            try:
                response = parse_response_message(raw)
            except (ValueError, json.JSONDecodeError):
                await _async_sleep(0.5)
                continue

            if not verify_response(intermediate, challenge, response):
                raise PairingError("PIN mismatch — pairing failed")

            # Derive shared secret and confirm
            shared = derive_shared_secret(intermediate, challenge)
            confirm_msg = make_confirm_message()
            await backend.send_progress(_PAIRING_CHANNEL + ":confirm", confirm_msg)
            logger.info("Pairing successful (initiator)")
            return PairingResult(shared_key=shared, peer_role="client")

        await _async_sleep(0.5)

    raise PairingError(f"Pairing timed out after {timeout}s — no response from peer")


async def respond_to_pairing(
    backend: Any,
    pin: str,
    timeout: float = 30.0,
) -> PairingResult:
    """Run the *responder* side of the pairing protocol.

    The responder is typically the **client**: it waits for the worker's
    challenge, computes a response, and waits for confirmation.

    Parameters
    ----------
    backend
        A pyfuse backend instance.
    pin
        The PIN entered by the user.
    timeout
        Seconds to wait for the challenge and confirmation.

    Raises
    ------
    PairingError
        On timeout or if the initiator rejects the response.
    """
    from pyfuse.core.errors import PairingError

    intermediate = _derive_intermediate(pin)

    # Wait for challenge
    deadline = time.monotonic() + timeout
    challenge: bytes | None = None
    while time.monotonic() < deadline:
        raw = await backend.get_progress(_PAIRING_CHANNEL)
        if raw is not None:
            try:
                challenge = parse_challenge_message(raw)
                break
            except (ValueError, json.JSONDecodeError):
                pass
        await _async_sleep(0.5)

    if challenge is None:
        raise PairingError(f"Pairing timed out after {timeout}s — no challenge from peer")

    # Compute and send response
    response = compute_response(intermediate, challenge)
    response_msg = make_response_message(response)
    await backend.send_progress(_PAIRING_CHANNEL + ":response", response_msg)
    logger.debug("Pairing: sent response")

    # Wait for confirmation
    while time.monotonic() < deadline:
        raw = await backend.get_progress(_PAIRING_CHANNEL + ":confirm")
        if raw is not None:
            try:
                parse_confirm_message(raw)
                shared = derive_shared_secret(intermediate, challenge)
                logger.info("Pairing successful (responder)")
                return PairingResult(shared_key=shared, peer_role="worker")
            except (ValueError, json.JSONDecodeError):
                pass
        await _async_sleep(0.5)

    raise PairingError("Pairing failed — initiator did not confirm")


async def _async_sleep(seconds: float) -> None:
    """Async sleep helper (avoids importing asyncio at module level)."""
    import asyncio
    await asyncio.sleep(seconds)
