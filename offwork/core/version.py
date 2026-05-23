"""Single source of truth for the package version.

When offwork is pip-installed, :func:`importlib.metadata.version` returns
the version baked into the installed distribution.  When running from a
source checkout (no ``offwork-*.dist-info`` available), we fall back to
parsing ``pyproject.toml`` directly so we never have to keep two version
strings in sync.
"""

import tomllib
from pathlib import Path
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version


def _read_pyproject_version() -> str:
    """Walk up from this file to find ``pyproject.toml`` and read its version."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "pyproject.toml"
        if candidate.exists():
            with candidate.open("rb") as f:
                data = tomllib.load(f)
            project = data.get("project") or {}
            ver = project.get("version")
            if isinstance(ver, str):
                return ver
            break
    return "0.0.0+unknown"


try:
    _VERSION: str = _pkg_version("offwork")
except PackageNotFoundError:
    _VERSION = _read_pyproject_version()
