"""Abstract base class for sandboxed function execution."""

import abc
from typing import Any


class SandboxExecutor(abc.ABC):
    """Execute reconstructed Python source inside a sandbox.

    Subclasses define *how* code is isolated.  The
    :class:`~pyfuse.worker.worker.Worker` delegates the compile → exec →
    call pipeline to an executor, keeping the rest of its logic (caching,
    dependency resolution, retry policy) unchanged.
    """

    @abc.abstractmethod
    async def execute(
        self,
        source: str,
        function_name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        owner_class: str | None = None,
    ) -> Any:
        """Execute *function_name* from *source* with the given arguments.

        Parameters
        ----------
        source
            Reconstructed Python source code (may define multiple functions
            and classes).
        function_name
            The short name of the target function (e.g. ``"f"``).
        args / kwargs
            Already-resolved positional and keyword arguments.
        owner_class
            If the target is a method, the short class name
            (e.g. ``"Greeter"``).

        Returns
        -------
        Any
            The value returned by the function.

        Raises
        ------
        Exception
            Re-raised from the sandboxed execution.
        """

    async def start(self) -> None:
        """Perform any one-time setup (e.g. boot a VM).

        The default implementation is a no-op.
        """

    async def stop(self) -> None:
        """Tear down resources (e.g. shut down a VM).

        The default implementation is a no-op.
        """

    async def __aenter__(self) -> "SandboxExecutor":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()
