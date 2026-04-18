"""Async event-loop helper — uses *uvloop* when available.

``uvloop`` is a fast, drop-in replacement for the default asyncio
event loop built on libuv.  When installed, it can deliver 2–4×
throughput improvements on I/O-bound workloads (exactly the pattern
used by the pyfuse worker).

Install it with::

    pip install pyfuse[fast]   # or: pip install uvloop

If *uvloop* is not installed, :func:`run` falls back transparently
to :func:`asyncio.run`.
"""

import asyncio
from typing import TypeVar
from collections.abc import Coroutine

_T = TypeVar("_T")

try:
    import uvloop as _uvloop
except ImportError:  # pragma: no cover
    _uvloop = None  # type: ignore[assignment]

_has_uvloop: bool = _uvloop is not None


def run(coro: Coroutine[object, object, _T]) -> _T:
    """Run *coro* with the fastest available event loop.

    Uses :func:`uvloop.run` when *uvloop* is installed, otherwise
    falls back to :func:`asyncio.run`.
    """
    if _has_uvloop:
        return _uvloop.run(coro)  # type: ignore[no-any-return]
    return asyncio.run(coro)
