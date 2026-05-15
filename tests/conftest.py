import importlib
import logging
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from seeya.graph.graph import Graph


def _configure_client_logging() -> None:
    """Mirror worker logging setup for the pytest process."""
    level_name = os.environ.get("SEEYA_LOG_LEVEL", "")
    if not level_name:
        return
    level = getattr(logging, level_name.upper(), None)
    if level is None:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s [client] %(message)s", datefmt="%H:%M:%S",
    ))
    seeya_logger = logging.getLogger("seeya")
    if not seeya_logger.handlers:
        seeya_logger.setLevel(level)
        seeya_logger.addHandler(handler)


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
