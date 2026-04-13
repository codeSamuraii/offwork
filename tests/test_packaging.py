"""Tests for PyPI packaging: version consistency and isolated installation."""
from __future__ import annotations

import subprocess
import sys
import venv
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow


def _project_root() -> Path:
    """Return the repository root (contains pyproject.toml)."""
    root = Path(__file__).resolve().parent.parent
    assert (root / "pyproject.toml").exists()
    return root


class TestVersionConsistency:
    """Ensure the package exposes a consistent version."""

    def test_version_is_string(self) -> None:
        from pyfuse.core.version import _VERSION

        assert isinstance(_VERSION, str)
        assert _VERSION  # non-empty

    def test_init_exports_version(self) -> None:
        import pyfuse

        assert hasattr(pyfuse, "__version__")
        assert isinstance(pyfuse.__version__, str)
        assert pyfuse.__version__  # non-empty

    def test_pyproject_matches_fallback(self) -> None:
        """The fallback version in version.py must match pyproject.toml."""
        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib  # type: ignore[no-redef]

        from pyfuse.core.version import _FALLBACK_VERSION

        pyproject = _project_root() / "pyproject.toml"
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
        assert data["project"]["version"] == _FALLBACK_VERSION


class TestIsolatedInstallation:
    """Install pyfuse in a temporary venv and verify it works."""

    def test_install_and_import_no_extras(self, tmp_path: Path) -> None:
        """Package installs and imports without optional dependencies."""
        venv_dir = tmp_path / "venv"
        venv.create(str(venv_dir), with_pip=True)

        if sys.platform == "win32":
            python = str(venv_dir / "Scripts" / "python.exe")
        else:
            python = str(venv_dir / "bin" / "python")

        root = _project_root()

        # Install the package (no extras)
        result = subprocess.run(
            [python, "-m", "pip", "install", str(root), "--quiet"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, f"pip install failed:\n{result.stderr}"

        # Verify import works
        result = subprocess.run(
            [
                python, "-c",
                "import pyfuse; "
                "print(pyfuse.__version__); "
                "print(pyfuse.serialize); "
                "print(pyfuse.trace); "
                "print(pyfuse.get_graph()); "
            ],
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
        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib  # type: ignore[no-redef]

        venv_dir = tmp_path / "venv"
        venv.create(str(venv_dir), with_pip=True)

        if sys.platform == "win32":
            python = str(venv_dir / "Scripts" / "python.exe")
        else:
            python = str(venv_dir / "bin" / "python")

        root = _project_root()

        pyproject = root / "pyproject.toml"
        with open(pyproject, "rb") as f:
            expected_version = tomllib.load(f)["project"]["version"]

        # Install
        result = subprocess.run(
            [python, "-m", "pip", "install", str(root), "--quiet"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, f"pip install failed:\n{result.stderr}"

        # Check version
        result = subprocess.run(
            [python, "-c", "import pyfuse; print(pyfuse.__version__)"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == expected_version

    def test_cli_entrypoint(self, tmp_path: Path) -> None:
        """The ``pyfuse`` CLI entry point is installed and functional."""
        venv_dir = tmp_path / "venv"
        venv.create(str(venv_dir), with_pip=True)

        if sys.platform == "win32":
            python = str(venv_dir / "Scripts" / "python.exe")
        else:
            python = str(venv_dir / "bin" / "python")

        root = _project_root()

        result = subprocess.run(
            [python, "-m", "pip", "install", str(root), "--quiet"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, f"pip install failed:\n{result.stderr}"

        # Run ``pyfuse info`` via the entry point
        result = subprocess.run(
            [python, "-m", "pyfuse", "info"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "pyfuse" in result.stdout
