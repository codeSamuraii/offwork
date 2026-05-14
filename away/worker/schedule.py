"""Schedule handle for recurring task management."""

from away.worker.backends.base import Backend


class ScheduleHandle:
    """Handle for a recurring schedule, allowing cancellation."""

    def __init__(self, schedule_id: str, backend: Backend) -> None:
        self._schedule_id = schedule_id
        self._backend = backend

    @property
    def schedule_id(self) -> str:
        return self._schedule_id

    async def cancel(self) -> None:
        """Cancel this recurring schedule.

        The worker will stop re-enqueuing new occurrences after the
        current one completes.
        """
        await self._backend.cancel_schedule(self._schedule_id)

    def __repr__(self) -> str:
        return f"ScheduleHandle(schedule_id={self._schedule_id!r})"
