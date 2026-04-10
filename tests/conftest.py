from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

from pyfuse.graph.graph import Graph


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
