import importlib
import logging
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from offwork.graph.graph import Graph


def _configure_client_logging() -> None:
    """Mirror worker logging setup for the pytest process."""
    level_name = os.environ.get("OFFWORK_LOG_LEVEL", "")
    if not level_name:
        return
    level = getattr(logging, level_name.upper(), None)
    if level is None:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s [client] %(message)s", datefmt="%H:%M:%S",
    ))
    offwork_logger = logging.getLogger("offwork")
    if not offwork_logger.handlers:
        offwork_logger.setLevel(level)
        offwork_logger.addHandler(handler)


_configure_client_logging()


def create_module(
    tmp_path: Path, name: str, source: str
) -> Any:
    """Write a .py file, add to sys.path, import and return the module."""
    file = tmp_path / f"{name}.py"
    file.write_text(source)
    if str(tmp_path) not in sys.path:
        sys.path.insert(0, str(tmp_path))
    if name in sys.modules:
        del sys.modules[name]
    return importlib.import_module(name)


@pytest.fixture(autouse=True)
def _reset_default_graph() -> None:
    Graph.reset_default()


@pytest.fixture(autouse=True)
def _reset_event_loop_after_sync_test(request: pytest.FixtureRequest) -> Iterator[None]:
    """Sync tests that call ``asyncio.run`` must not leave a loop for gc to warn on."""
    yield
    if request.node.get_closest_marker("asyncio") is not None:
        return
    import asyncio

    asyncio.set_event_loop(None)


@pytest.fixture(scope="session", autouse=True)
def _disconnect_backend_after_session() -> Iterator[None]:
    """Close any global backend before interpreter teardown (avoids atexit/gc leaks)."""
    yield
    import asyncio

    import offwork.worker.remote as remote

    if remote._active_backend is None:
        return
    try:
        asyncio.run(remote.disconnect())
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(remote.disconnect())
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
    remote._atexit_registered = False
