"""Tests for PyPI packaging: version consistency and isolated installation."""

import subprocess
import sys
import venv
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

import offwork
import pytest

from offwork.core.version import _VERSION


def _project_root() -> Path:
    """Return the repository root (contains pyproject.toml)."""
    root = Path(__file__).resolve().parent.parent
    assert (root / "pyproject.toml").exists()
    return root


def _venv_python(venv_dir: Path) -> str:
    """Return the path to the Python executable inside a venv."""
    if sys.platform == "win32":
        return str(venv_dir / "Scripts" / "python.exe")
    return str(venv_dir / "bin" / "python")


def _create_venv_and_install(tmp_path: Path) -> str:
    """Create a venv at *tmp_path*/venv, install offwork, return python path."""
    venv_dir = tmp_path / "venv"
    venv.create(str(venv_dir), with_pip=True)
    python = _venv_python(venv_dir)

    result = subprocess.run(
        [python, "-m", "pip", "install", str(_project_root()), "--quiet"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"pip install failed:\n{result.stderr}"
    return python


class TestVersionConsistency:
    """Ensure the package exposes a consistent version."""

    def test_version_is_string(self) -> None:
        assert isinstance(_VERSION, str)
        assert _VERSION  # non-empty

    def test_init_exports_version(self) -> None:
        assert hasattr(offwork, "__version__")
        assert isinstance(offwork.__version__, str)
        assert offwork.__version__  # non-empty

    def test_source_fallback_reads_pyproject(self) -> None:
        """The source-checkout fallback parses pyproject.toml directly."""
        from offwork.core.version import _read_pyproject_version

        pyproject = _project_root() / "pyproject.toml"
        with open(pyproject, "rb") as f:
            expected = tomllib.load(f)["project"]["version"]
        assert _read_pyproject_version() == expected


class TestIsolatedInstallation:
    """Install offwork in a temporary venv and verify it works."""

    def test_install_and_import_no_extras(self, tmp_path: Path) -> None:
        """Package installs and imports without optional dependencies."""
        python = _create_venv_and_install(tmp_path)

        script = (
            "import offwork\n"
            "print(offwork.__version__)\n"
            "print(offwork.serialize)\n"
            "print(offwork.task)\n"
            "print(offwork.get_graph())\n"
        )
        result = subprocess.run(
            [python, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"Import failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

        lines = result.stdout.strip().splitlines()
        # First line is the version
        assert lines[0]  # non-empty version string

    def test_install_version_matches(self, tmp_path: Path) -> None:
        """Installed package version matches pyproject.toml."""
        python = _create_venv_and_install(tmp_path)

        pyproject = _project_root() / "pyproject.toml"
        with open(pyproject, "rb") as f:
            expected_version = tomllib.load(f)["project"]["version"]

        result = subprocess.run(
            [python, "-c", "import offwork; print(offwork.__version__)"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == expected_version

    def test_cli_entrypoint(self, tmp_path: Path) -> None:
        """The ``offwork`` CLI entry point is installed and functional."""
        python = _create_venv_and_install(tmp_path)

        # Run ``offwork info`` via the entry point
        result = subprocess.run(
            [python, "-m", "offwork", "info"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "offwork" in result.stdout
