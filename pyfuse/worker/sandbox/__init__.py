"""Sandbox subsystem for isolating function execution.

The sandbox provides a :class:`SandboxExecutor` abstraction that the
:class:`~pyfuse.worker.worker.Worker` uses to run reconstructed Python
code.  Two implementations are shipped:

:class:`NoopExecutor`
    Runs code in the host process — identical to the pre-sandbox
    behaviour.

:class:`VMExecutor`
    Runs code inside a lightweight Apple-Silicon micro-VM managed by
    `tart <https://github.com/cirruslabs/tart>`_.

Quick start::

    from pyfuse.worker.sandbox import create_executor, SandboxConfig

    executor = create_executor(SandboxConfig(enabled=True))
    result = await executor.execute(source, "my_func", (arg1,), {})
"""

from pyfuse.worker.sandbox.base import SandboxExecutor
from pyfuse.worker.sandbox.config import SandboxConfig
from pyfuse.worker.sandbox.noop import NoopExecutor


def create_executor(config: SandboxConfig | None = None) -> SandboxExecutor:
    """Instantiate the right executor based on *config*.

    When ``config`` is *None* or ``config.enabled`` is ``False``, a
    :class:`NoopExecutor` is returned (zero overhead).

    When ``config.enabled`` is ``True``, a :class:`VMExecutor` backed
    by *tart* is returned.
    """
    if config is None or not config.enabled:
        return NoopExecutor()

    from pyfuse.worker.sandbox.vm import VMExecutor

    return VMExecutor(config)


__all__ = [
    "SandboxConfig",
    "SandboxExecutor",
    "NoopExecutor",
    "create_executor",
]
