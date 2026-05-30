"""Shared timeout type and resolution helper used across the public API."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

# ── Named sentinels ──────────────────────────────────────────────────────────

type WaitForever = Literal[False] | Literal[-1]
"""Sentinel: block until the operation completes, with no deadline."""

type ReturnImmediately = Literal[True] | Literal[0]
"""Sentinel: return as soon as possible (non-blocking / fast-poll)."""

# ── Public timeout type ───────────────────────────────────────────────────────

type TimeoutIn = float | timedelta | WaitForever | ReturnImmediately
"""A duration accepted by every wait-style method in the public API.

Interpretation
--------------
``False`` or ``-1``
    Block indefinitely until the operation completes.
``True`` or ``0``
    Return as soon as possible (non-blocking or single fast poll).
``timedelta``
    Wait at most this duration.
``float`` (positive)
    Wait at most this many seconds.
"""


# ── Internal helper ───────────────────────────────────────────────────────────

def resolve_timeout(t: TimeoutIn) -> float | None:
    """Convert a :data:`TimeoutIn` value to seconds (``float``) or ``None``.

    Returns
    -------
    None
        Wait forever (corresponds to ``False`` or ``-1``).
    0.0
        Non-blocking / return immediately (corresponds to ``True`` or ``0``).
    positive float
        Maximum seconds to wait.
    """
    # bool is a subtype of int — check identity before numeric equality
    # so that False is not mistaken for 0 and True is not mistaken for 1.
    if t is False:
        return None
    if t is True:
        return 0.0
    if t == -1:
        return None
    if t == 0:
        return 0.0
    if isinstance(t, timedelta):
        return t.total_seconds()
    return float(t)
