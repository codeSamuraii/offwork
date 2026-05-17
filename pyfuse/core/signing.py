"""Cryptographic task signing and verification.

After a client and worker are paired (see :mod:`pyfuse.core.pairing`),
they share a secret key.  The client uses it to produce an HMAC-SHA256
signature over the serialized task payload; the worker verifies that
signature before executing the task.

All primitives are stdlib-only (``hashlib``, ``hmac``, ``json``).
"""

import hmac
import json
import hashlib
import logging
from typing import Any

from pyfuse.core.errors import SignatureError

logger = logging.getLogger(__name__)


def compute_signature(payload: str, key: bytes) -> str:
    """Return a hex-encoded HMAC-SHA256 signature of *payload*.

    Parameters
    ----------
    payload
        The string to sign (typically the JSON body of a task).
    key
        Shared secret derived from the pairing process.
    """
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_signature(payload: str, signature: str, key: bytes) -> bool:
    """Verify an HMAC-SHA256 *signature* over *payload*.

    Uses :func:`hmac.compare_digest` for constant-time comparison.
    """
    expected = hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# -- Signed JSON helpers ----------------------------------------------------


def sign_json(data: dict[str, Any], key: bytes) -> str:
    """Serialize *data* to JSON, attach an HMAC-SHA256 signature, and return the envelope.

    The returned JSON has the shape::

        {"payload": "<inner-json>", "signature": "<hex>"}
    """
    payload = json.dumps(data, separators=(",", ":"), sort_keys=True)
    sig = compute_signature(payload, key)
    return json.dumps({"payload": payload, "signature": sig})


def verify_and_load_json(envelope_json: str, key: bytes) -> dict[str, Any]:
    """Parse *envelope_json*, verify the signature, and return the inner data.

    Raises
    ------
    SignatureError
        If the signature is missing or invalid.
    """
    try:
        envelope = json.loads(envelope_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise SignatureError(f"Invalid signed envelope: {exc}") from exc

    payload = envelope.get("payload")
    signature = envelope.get("signature")
    if payload is None or signature is None:
        raise SignatureError("Envelope missing 'payload' or 'signature' field")

    if not verify_signature(payload, signature, key):
        raise SignatureError("HMAC signature verification failed")

    return json.loads(payload)  # type: ignore[no-any-return]


# -- Key derivation ---------------------------------------------------------


def derive_key(shared_secret: bytes, context: str = "pyfuse-task-signing") -> bytes:
    """Derive a 32-byte HMAC signing key from a shared secret.

    Uses HKDF-like expansion via ``HMAC-SHA256(secret, context)``.
    """
    return hmac.new(shared_secret, context.encode("utf-8"), hashlib.sha256).digest()
