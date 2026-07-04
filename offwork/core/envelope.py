"""Signed task envelope: build + verify.

The signed envelope is a strict superset of the raw task payload
emitted by :meth:`Task._to_dict`.  It carries:

- ``client_id`` — 32-hex-char stable per-machine identifier
- ``iat``       — issued-at Unix timestamp (float)
- ``nonce``     — 16-hex random per-task value
- ``pubkey``    — 64-hex Ed25519 public key
- ``ed_sig``    — 128-hex Ed25519 signature
- ``signature`` — hex HMAC-SHA256, key = derive_key(root_token,
                  "offwork-v1|client:" + client_id)

Both signatures cover the canonical JSON of all *other* envelope keys
(sorted, no whitespace).  The worker verifies in this order:

    1. Reject if client_id is on the denylist.
    2. Reject if ``|now - iat| > clock_skew``.
    3. Reject if ``(client_id, nonce)`` has already been seen.
    4. Verify HMAC under the per-client derived key.
    5. TOFU-pin Ed25519 public key, then verify Ed25519 signature.
    6. Record the nonce.

This module is stateless except via the ``KnownClients`` and
``NonceLRU`` objects passed in by the caller (typically constructed
once in ``serve``).
"""

import os
import json
import time
import logging
from typing import Any

from offwork.core.task import Task, _TaskEncoder
from offwork.core.errors import (
    ReplayError,
    StaleTaskError,
    SignatureError,
    ClientRevokedError,
)
from offwork.core.signing import NonceLRU, derive_key, compute_signature, verify_signature
from offwork.core.clients import KnownClients
from offwork.core import ed25519

logger = logging.getLogger(__name__)

DEFAULT_CLOCK_SKEW = 300.0  # seconds

_PER_CLIENT_CONTEXT = "offwork-v1|client:"


def per_client_key(root_token: bytes, client_id: str) -> bytes:
    """Derive the HMAC key for a specific client_id from the root token."""
    return derive_key(root_token, _PER_CLIENT_CONTEXT + client_id)


def _canonical_payload(envelope: dict[str, Any]) -> str:
    """Canonical JSON of *envelope* with both signature fields stripped."""
    body = {k: v for k, v in envelope.items() if k not in ("signature", "ed_sig")}
    return json.dumps(body, cls=_TaskEncoder, separators=(",", ":"), sort_keys=True)


def build_signed_envelope(
    task: Task,
    *,
    root_token: bytes,
    client_id: str,
    identity_seed: bytes,
    public_key: bytes,
    now: float | None = None,
) -> str:
    """Build a fully-signed task envelope as a JSON string.

    The client side calls this once per submission.  See module docstring
    for the envelope layout.
    """
    body = task._to_dict()
    body["client_id"] = client_id
    body["iat"] = now if now is not None else time.time()
    body["nonce"] = os.urandom(8).hex()
    body["pubkey"] = public_key.hex()

    payload = json.dumps(body, cls=_TaskEncoder, separators=(",", ":"), sort_keys=True)

    hmac_key = per_client_key(root_token, client_id)
    body["signature"] = compute_signature(payload, hmac_key)
    body["ed_sig"] = ed25519.sign(payload.encode("utf-8"), identity_seed).hex()

    return json.dumps(body, cls=_TaskEncoder)


def verify_task_envelope(
    envelope_json: str,
    *,
    root_token: bytes,
    known_clients: KnownClients,
    nonce_lru: NonceLRU,
    clock_skew: float = DEFAULT_CLOCK_SKEW,
    now: float | None = None,
) -> Task:
    """Verify a signed envelope and return the inner :class:`Task`.

    Raises subclasses of :class:`~offwork.core.errors.SignatureError`
    on every kind of rejection so existing ``except SignatureError``
    handlers keep working.
    """
    try:
        data = json.loads(envelope_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise SignatureError(f"Invalid envelope JSON: {exc}") from exc

    client_id = data.get("client_id")
    iat = data.get("iat")
    nonce = data.get("nonce")
    pubkey_hex = data.get("pubkey")
    ed_sig_hex = data.get("ed_sig")
    hmac_sig = data.get("signature")

    if not isinstance(client_id, str) or not isinstance(nonce, str):
        raise SignatureError("Envelope missing client_id or nonce")
    if not isinstance(pubkey_hex, str) or not isinstance(ed_sig_hex, str):
        raise SignatureError("Envelope missing Ed25519 public key or signature")
    if not isinstance(hmac_sig, str):
        raise SignatureError("Envelope missing HMAC signature")
    if not isinstance(iat, (int, float)):
        raise SignatureError("Envelope missing or invalid iat")

    # 1. Denylist
    if known_clients.is_revoked(client_id):
        raise ClientRevokedError(f"Client {client_id[:8]} is revoked")

    # 2. Freshness
    t = now if now is not None else time.time()
    if abs(t - float(iat)) > clock_skew:
        raise StaleTaskError(
            f"Task timestamp outside clock-skew window ({abs(t - float(iat)):.1f}s)"
        )

    # 3. Replay
    if not nonce_lru.check_and_add(client_id, nonce, now=t):
        raise ReplayError(f"Nonce already seen for client {client_id[:8]}")

    # Re-canonicalise without signature fields to validate.
    payload = _canonical_payload(data)

    # 4. HMAC under per-client key
    hmac_key = per_client_key(root_token, client_id)
    if not verify_signature(payload, hmac_sig, hmac_key):
        raise SignatureError("HMAC signature verification failed")

    # 5. Ed25519 + TOFU pin
    try:
        pubkey = bytes.fromhex(pubkey_hex)
        ed_sig = bytes.fromhex(ed_sig_hex)
    except ValueError as exc:
        raise SignatureError(f"Malformed Ed25519 material: {exc}") from exc
    if not ed25519.verify(payload.encode("utf-8"), ed_sig, pubkey):
        raise SignatureError("Ed25519 signature verification failed")
    # TOFU pin (raises IdentityMismatchError on mismatch)
    known_clients.register_or_verify(client_id, pubkey_hex)

    return Task(
        graph_json=data["graph"],
        function_name=data["function"],
        args=tuple(data.get("args", ())),
        kwargs=data.get("kwargs", {}),
        task_id=data.get("id", ""),
        timeout=data.get("timeout"),
        retries=data.get("retries", 0),
        retry_delay=data.get("retry_delay", 1.0),
        scheduled_at=data.get("scheduled_at"),
        recur_interval=data.get("recur_interval"),
        recur_deadline=data.get("recur_deadline"),
        recur_remaining=data.get("recur_remaining"),
        schedule_id=data.get("schedule_id"),
        throttle=data.get("throttle"),
        storage=bool(data.get("storage", False)),
    )
