"""HMAC primitives and the worker-side nonce cache.

The high-level signed-envelope flow lives in :mod:`offwork.core.envelope`.
This module only provides the cryptographic building blocks used there:

- :func:`compute_signature` / :func:`verify_signature` — constant-time
  HMAC-SHA256 over a string payload.
- :func:`derive_key` — HKDF-style 32-byte subkey derivation from a
  shared secret and a context label.
- :class:`NonceLRU` — TTL- and capacity-bounded ``(client_id, nonce)``
  cache used by the worker to reject replays.

All primitives are stdlib-only.
"""

import hmac
import time
import hashlib
import logging
import threading
from collections import OrderedDict

logger = logging.getLogger(__name__)


def compute_signature(payload: str, key: bytes) -> str:
    """Return a hex-encoded HMAC-SHA256 signature of *payload*."""
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_signature(payload: str, signature: str, key: bytes) -> bool:
    """Verify an HMAC-SHA256 *signature* over *payload* in constant time."""
    expected = hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def derive_key(shared_secret: bytes, context: str = "offwork-task-signing") -> bytes:
    """Derive a 32-byte subkey from *shared_secret* and *context*.

    Uses HKDF-like expansion via ``HMAC-SHA256(secret, context)``.
    """
    return hmac.new(shared_secret, context.encode("utf-8"), hashlib.sha256).digest()


class NonceLRU:
    """TTL- and capacity-bounded set of ``(client_id, nonce)`` pairs.

    The worker uses this to reject replayed task envelopes.  Entries age
    out after *ttl* seconds; the cache is capped at *capacity* to bound
    memory.  Thread-safe; operations are O(1) amortised.
    """

    def __init__(self, ttl: float = 600.0, capacity: int = 100_000) -> None:
        self._ttl = ttl
        self._capacity = capacity
        self._items: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._lock = threading.Lock()

    def _evict(self, now: float) -> None:
        while self._items:
            _key, ts = next(iter(self._items.items()))
            if now - ts > self._ttl:
                self._items.popitem(last=False)
            else:
                break
        while len(self._items) > self._capacity:
            self._items.popitem(last=False)

    def check_and_add(
        self, client_id: str, nonce: str, now: float | None = None
    ) -> bool:
        """Return True if *(client_id, nonce)* is fresh; record it.

        Returns False (and does not modify the cache) if the pair has
        been seen within the TTL window.
        """
        t = now if now is not None else time.time()
        key = (client_id, nonce)
        with self._lock:
            self._evict(t)
            if key in self._items:
                return False
            self._items[key] = t
            return True

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
