"""Schedule handle for recurring task management."""

import asyncio
import time

from offwork.worker.backends.base import Backend


class ScheduleHandle:
    """Handle for a recurring schedule, allowing cancellation."""

    def __init__(self, schedule_id: str, backend: Backend) -> None:
        self._schedule_id = schedule_id
        self._backend = backend

    @property
    def schedule_id(self) -> str:
        return self._schedule_id

    async def cancel(self, wait: bool | float = False) -> bool:
        """Cancel this recurring schedule.

        The worker stops re-enqueuing new occurrences after the
        current one completes.

        Parameters
        ----------
        wait
            If ``False`` (default), return immediately. If ``True``,
            block until the backend confirms the schedule is marked
            cancelled (default 30s timeout). If a number, wait that
            many seconds.

        Returns
        -------
        bool
            ``True`` if cancellation was acknowledged (or
            ``wait=False``). ``False`` on timeout.
        """
        await self._backend.cancel_schedule(self._schedule_id)
        if wait is False:
            return True
        timeout = 30.0 if wait is True else float(wait)
        deadline = time.monotonic() + timeout
        while True:
            if await self._backend.is_schedule_cancelled(self._schedule_id):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.5)

    def __repr__(self) -> str:
        return f"ScheduleHandle(schedule_id={self._schedule_id!r})"
