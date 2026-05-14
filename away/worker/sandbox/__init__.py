"""Docker sandbox for isolating function execution.

When ``--sandbox`` is enabled, the :class:`~away.worker.worker.Worker`
delegates the ``exec → call`` step to a guest agent running inside a
Docker container.  Everything else (caching, dependency resolution,
retry policy) stays on the host.

Quick start::

    from away.worker.sandbox import DockerSandbox

    async with DockerSandbox() as sandbox:
        result = await sandbox.execute(source, "my_func", (arg1,), {})
"""

from away.worker.sandbox.docker import DockerSandbox

__all__ = ["DockerSandbox"]
