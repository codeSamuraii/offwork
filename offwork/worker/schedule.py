"""Schedule handle for recurring task management."""

import asyncio
import time

from offwork.core._timeout import TimeoutIn, resolve_timeout
from offwork.worker.backends.base import Backend


class ScheduleHandle:
    """Handle for a recurring schedule, allowing cancellation."""

    def __init__(self, schedule_id: str, backend: Backend) -> None:
        self._schedule_id = schedule_id
        self._backend = backend

    @property
    def schedule_id(self) -> str:
        """The unique identifier for this recurring schedule."""
        return self._schedule_id

    async def cancel(self, timeout: TimeoutIn = 0.0) -> bool:
        """Cancel this recurring schedule.

        The worker stops re-enqueuing new occurrences after the current
        one completes.

        Parameters
        ----------
        timeout
            How long to wait for the backend to confirm cancellation.
            Defaults to ``0.0`` (fire-and-forget: signal sent, return
            immediately).  ``False`` or ``-1`` wait indefinitely.
            ``True`` or ``0`` are equivalent to the default.
            See :data:`~offwork.TimeoutIn` for the full convention.

        Returns
        -------
        bool
            ``True`` if cancellation was acknowledged or
            *timeout* is non-blocking.  ``False`` if *timeout* elapsed
            before confirmation arrived.
        """
        await self._backend.cancel_schedule(self._schedule_id)
        resolved = resolve_timeout(timeout)
        if resolved == 0.0:
            return True
        deadline = None if resolved is None else time.monotonic() + resolved
        while True:
            if await self._backend.is_schedule_cancelled(self._schedule_id):
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.5)

    def __repr__(self) -> str:
        return f"ScheduleHandle(schedule_id={self._schedule_id!r})"
