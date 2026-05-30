"""Protocol types for ``@offwork.task``-decorated functions."""

from datetime import datetime, timedelta
from typing import Any, TypeVar, Protocol, ParamSpec, overload
from collections.abc import Callable

from offwork.worker.result import Result
from offwork.worker.schedule import ScheduleHandle

P = ParamSpec("P")
R = TypeVar("R")


class TracedFunction(Protocol[P, R]):
    """A function decorated with ``@offwork.task``, with remote execution methods.

    Direct call
    -----------
    Calling the function normally (``func(*args)``) executes it locally,
    exactly as if ``@offwork.task`` were not present.

    Remote execution
    ----------------
    :meth:`submit`
        Submit to a remote worker; return a :class:`~offwork.Result` handle.
        Pass scheduling keywords to run once in the future or on a recurring
        schedule (returns :class:`~offwork.ScheduleHandle` when *run_every*
        is given).
    :meth:`run`
        Submit and ``await`` the result in one call.
    :meth:`map`
        Submit the same function with multiple argument-tuples in parallel.
    """

    __offwork_traced__: bool
    __wrapped__: Callable[P, R]

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R: ...

    # -- overload: run_every present → ScheduleHandle -------------------------

    @overload
    async def submit(
        self,
        *args: Any,
        run_every: timedelta | float,
        run_at: None = ...,
        run_in: None = ...,
        _start_at: datetime | None = ...,
        run_for: timedelta | float | None = ...,
        max_runs: int | None = ...,
        backend: Any = ...,
        **kwargs: Any,
    ) -> ScheduleHandle: ...

    # -- overload: no run_every → Result ---------------------------------------

    @overload
    async def submit(
        self,
        *args: Any,
        run_every: None = ...,
        run_at: datetime | None = ...,
        run_in: timedelta | float | None = ...,
        backend: Any = ...,
        **kwargs: Any,
    ) -> Result: ...

    async def submit(self, *args: Any, **kwargs: Any) -> Result | ScheduleHandle:
        """Submit the function to a remote worker.

        Parameters
        ----------
        *args
            Positional arguments forwarded to the function.
        run_at
            :class:`~datetime.datetime` — schedule a one-shot run at this
            point in time.
        run_in
            :class:`~datetime.timedelta` or ``float`` (seconds) — schedule
            a one-shot run after this delay.
        run_every
            :class:`~datetime.timedelta` or ``float`` (seconds) — run on a
            recurring schedule at this interval.  Returns a
            :class:`~offwork.ScheduleHandle` instead of a :class:`~offwork.Result`.
        _start_at
            First occurrence for *run_every* schedules.
        run_for
            Stop recurring after this wall-clock duration (*run_every* only).
        max_runs
            Stop recurring after this many executions (*run_every* only).
        backend
            Override the global backend for this submission.
        **kwargs
            Keyword arguments forwarded to the function.

        Returns
        -------
        Result
            When called without *run_every*.
        ScheduleHandle
            When called with *run_every*.
        """
        ...

    async def run(self, *args: P.args, **kwargs: P.kwargs) -> Any:
        """Submit and immediately ``await`` the result.

        Equivalent to ``await (await func.submit(*args, **kwargs))``.
        """
        ...

    async def map(self, args_list: list[tuple[Any, ...]], **kwargs: Any) -> list[Any]:
        """Submit the function for each argument-tuple and collect all results.

        Equivalent to::

            await asyncio.gather(*(func.run(*a, **kwargs) for a in args_list))
        """
        ...


class TraceDecorator(Protocol):
    """The ``@offwork.task`` decorator when called with keyword arguments."""

    def __call__(self, func: Callable[P, R]) -> TracedFunction[P, R]: ...