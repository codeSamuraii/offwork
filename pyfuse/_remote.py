from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from typing import Any

from pyfuse._backend import Backend, RedisBackend
from pyfuse._result import FuseResult, ResultEnvelope
from pyfuse._task import Task

logger = logging.getLogger(__name__)

_active_backend: Backend | None = None


def connect(url: str, **kwargs: Any) -> Backend:
    """Configure the global transport backend.

    Parameters
    ----------
    url
        Backend URL.  Supported schemes:

        - ``redis://`` / ``rediss://`` — :class:`RedisBackend`

    **kwargs
        Passed to the backend constructor.

    Returns
    -------
    Backend
        The connected backend instance.
    """
    global _active_backend
    scheme = url.split("://", 1)[0].lower()
    if scheme in ("redis", "rediss"):
        _active_backend = RedisBackend(url, **kwargs)
    else:
        raise ValueError(
            f"Unknown backend scheme: {scheme!r}. "
            f"Supported: redis://, rediss://"
        )
    logger.info("Connected to backend: %s", url)
    return _active_backend


def disconnect() -> None:
    """Close and clear the global backend."""
    global _active_backend
    if _active_backend is not None:
        _active_backend.close()
        _active_backend = None
        logger.info("Disconnected from backend")


def get_backend() -> Backend:
    """Return the active backend, or raise if none is configured."""
    if _active_backend is None:
        raise RuntimeError(
            "No backend connected. Call pyfuse.connect('redis://...') first."
        )
    return _active_backend


def submit_remote(
    func: Callable[..., object],
    wrapper: Callable[..., object],
    *args: Any,
    **kwargs: Any,
) -> FuseResult:
    """Pack and submit a function to the remote backend.

    Called internally by ``traced_func.run(...)``.
    """
    from pyfuse._graph import FuseGraph

    backend = get_backend()

    unwrapped = inspect.unwrap(func)
    function_name = f"{unwrapped.__module__}.{unwrapped.__qualname__}"
    graph_json = FuseGraph.default().serialize(wrapper)
    task = Task(
        graph_json=graph_json,
        function_name=function_name,
        args=args,
        kwargs=kwargs,
    )

    backend.submit(task.to_json())
    logger.info("Submitted task %s for %s", task.task_id, function_name)
    return FuseResult(task.task_id, backend)


def serve(
    url: str,
    *,
    auto_install: bool = True,
    import_to_package: dict[str, str] | None = None,
) -> None:
    """Start a worker loop that pops tasks from the backend and executes them.

    Parameters
    ----------
    url
        Backend URL (e.g. ``redis://localhost:6379``).
    auto_install
        Automatically install missing third-party dependencies via pip.
    import_to_package
        Extra import-name to pip-package-name mappings.
    """
    from pyfuse._worker import FuseWorker

    backend = connect(url)
    worker = FuseWorker(
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
