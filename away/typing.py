"""Protocol types for ``@trace``-decorated functions."""

from datetime import datetime, timedelta
from typing import Any, TypeVar, Protocol, ParamSpec
from collections.abc import Callable

from away.worker.result import Result
from away.worker.schedule import ScheduleHandle

P = ParamSpec("P")
R = TypeVar("R")


class TracedFunction(Protocol[P, R]):
    """A function decorated with ``@trace``, with remote execution methods."""

    __away_traced__: bool
    __wrapped__: Callable[P, R]

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R: ...

    async def start(self, *args: P.args, **kwargs: P.kwargs) -> Result: ...

    async def run(self, *args: P.args, **kwargs: P.kwargs) -> Any: ...

    async def map(self, args_list: list[tuple[Any, ...]], **kwargs: Any) -> list[Any]: ...

    async def start_at(self, dt: datetime, *args: P.args, **kwargs: P.kwargs) -> Result: ...

    async def run_at(self, dt: datetime, *args: P.args, **kwargs: P.kwargs) -> Any: ...

    async def start_in(self, delay: timedelta | float, *args: P.args, **kwargs: P.kwargs) -> Result: ...

    async def run_in(self, delay: timedelta | float, *args: P.args, **kwargs: P.kwargs) -> Any: ...

    async def run_every(
        self,
        frequency: timedelta | float,
        *args: Any,
        _start_at: datetime | None = ...,
        **kwargs: Any,
    ) -> ScheduleHandle: ...


class TraceDecorator(Protocol):
    """The ``@trace`` decorator when called with keyword arguments."""

    def __call__(self, func: Callable[P, R]) -> TracedFunction[P, R]: ...