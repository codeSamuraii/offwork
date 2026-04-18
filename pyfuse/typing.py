"""Protocol types for ``@trace``-decorated functions."""

from collections.abc import Callable
from typing import Any, ParamSpec, Protocol, TypeVar

from pyfuse.worker.result import Result

P = ParamSpec("P")
R = TypeVar("R")


class TracedFunction(Protocol[P, R]):
    """A function decorated with ``@trace``, with remote execution methods."""

    __pyfuse_traced__: bool
    __wrapped__: Callable[P, R]

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R: ...

    async def start(self, *args: P.args, **kwargs: P.kwargs) -> Result: ...

    async def run(self, *args: P.args, **kwargs: P.kwargs) -> Any: ...

    async def map(self, args_list: list[tuple[Any, ...]], **kwargs: Any) -> list[Any]: ...


class TraceDecorator(Protocol):
    """The ``@trace`` decorator when called with keyword arguments."""

    def __call__(self, func: Callable[P, R]) -> TracedFunction[P, R]: ...