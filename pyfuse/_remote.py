from __future__ import annotations

import inspect
import logging
import os
from collections.abc import Callable
from typing import Any

from pyfuse._backend import Backend, RedisBackend
from pyfuse._result import Result, ResultEnvelope
from pyfuse._task import Task

logger = logging.getLogger(__name__)

_active_backend: Backend | None = None

_ENV_VAR = "PYFUSE_BACKEND"


def _resolve_url(url: str | None) -> str:
    """Return *url* if given, otherwise read from the environment variable."""
    if url is not None:
        return url
    env_url = os.environ.get(_ENV_VAR)
    if env_url:
        return env_url
    raise ValueError(
        "No backend URL provided. Pass a URL or set the "
        f"{_ENV_VAR} environment variable."
    )


def _create_backend(url: str, **kwargs: Any) -> Backend:
    """Create a backend instance from a URL."""
    scheme = url.split("://", 1)[0].lower()
    if scheme in ("redis", "rediss"):
        return RedisBackend(url, **kwargs)
    if scheme == "shm":
        from pyfuse._shm_backend import SharedMemoryBackend

        return SharedMemoryBackend(url, **kwargs)
    raise ValueError(
        f"Unknown backend scheme: {scheme!r}. "
        f"Supported: redis://, rediss://, shm://"
    )


def connect(url: str | None = None, **kwargs: Any) -> Backend:
    """Configure the global transport backend.

    Parameters
    ----------
    url
        Backend URL.  Supported schemes:

        - ``redis://`` / ``rediss://`` — :class:`RedisBackend`
        - ``shm://`` — :class:`SharedMemoryBackend` (same-machine IPC)

        When *None*, the ``PYFUSE_BACKEND`` environment variable is used.

    **kwargs
        Passed to the backend constructor.

    Returns
    -------
    Backend
        The connected backend instance.
    """
    global _active_backend
    resolved = _resolve_url(url)
    _active_backend = _create_backend(resolved, **kwargs)
    logger.info("Connected to backend: %s", resolved)
    return _active_backend


def disconnect() -> None:
    """Close and clear the global backend."""
    global _active_backend
    if _active_backend is not None:
        _active_backend.close()
        _active_backend = None
        logger.info("Disconnected from backend")


def get_backend() -> Backend:
    """Return the active backend, or raise if none is configured.

    If no backend has been configured via :func:`connect`, the
    ``PYFUSE_BACKEND`` environment variable is checked and used
    to auto-connect.
    """
    global _active_backend
    if _active_backend is None:
        env_url = os.environ.get(_ENV_VAR)
        if env_url:
            connect(env_url)
        else:
            raise RuntimeError(
                "No backend connected. Call pyfuse.connect('redis://...') "
                f"or set the {_ENV_VAR} environment variable."
            )
    return _active_backend  # type: ignore[return-value]


def submit_remote(
    func: Callable[..., object],
    wrapper: Callable[..., object],
    *args: Any,
    _backend: str | Backend | None = None,
    **kwargs: Any,
) -> Result:
    """Pack and submit a function to the remote backend.

    Called internally by ``traced_func.run(...)``.
    """
    from pyfuse._graph import Graph

    if isinstance(_backend, str):
        backend = _create_backend(_backend)
    elif isinstance(_backend, Backend):
        backend = _backend
    else:
        backend = get_backend()

    unwrapped = inspect.unwrap(func)
    function_name = f"{unwrapped.__module__}.{unwrapped.__qualname__}"
    graph_json = Graph.default().serialize(wrapper)
    task = Task(
        graph_json=graph_json,
        function_name=function_name,
        args=args,
        kwargs=kwargs,
    )

    backend.submit(task.to_json())
    logger.info("Submitted task %s for %s", task.task_id, function_name)
    return Result(task.task_id, backend)


def serve(
    url: str | None = None,
    *,
    auto_install: bool = True,
    import_to_package: dict[str, str] | None = None,
) -> None:
    """Start a worker loop that pops tasks from the backend and executes them.

    Parameters
    ----------
    url
        Backend URL (e.g. ``redis://localhost:6379``).
        When *None*, the ``PYFUSE_BACKEND`` environment variable is used.
    auto_install
        Automatically install missing third-party dependencies via pip.
    import_to_package
        Extra import-name to pip-package-name mappings.
    """
    from pyfuse._worker import Worker

    backend = connect(url)
    worker = Worker(
        auto_install=auto_install,
        import_to_package=import_to_package,
    )

    print(f"Worker listening on {url}", flush=True)
    print("Press Ctrl+C to stop.\n", flush=True)

    try:
        for task_json in backend.listen():
            task = Task.from_json(task_json)
            logger.info("Received task %s: %s", task.task_id, task.function_name)

            try:
                result = worker.run(task)
                envelope = ResultEnvelope.success(task.task_id, result)
            except Exception as exc:
                logger.exception("Task %s failed", task.task_id)
                envelope = ResultEnvelope.failure(task.task_id, exc)

            backend.send_result(task.task_id, envelope.to_json())

            cache = worker.cache_info()
            status = "ok" if envelope.status == "ok" else "error"
            print(
                f"  [{status}] {task.function_name:<30} "
                f"cache size: {cache['size']}",
                flush=True,
            )
    except KeyboardInterrupt:
        print("\nWorker stopped.")
    finally:
        disconnect()
