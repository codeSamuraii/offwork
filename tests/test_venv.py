"""Tests for temporary virtual environment management."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from pyfuse._venv import (
    TempVenv,
    _DEFAULT_PREFIX,
    _find_project_root,
    _venv_python,
    cleanup_stale_venvs,
    temp_venv,
)

pytestmark = pytest.mark.slow


class TestFindProjectRoot:
    def test_finds_root(self) -> None:
        root = _find_project_root()
        assert root is not None
        assert (root / "pyproject.toml").exists()

    def test_root_contains_pyfuse(self) -> None:
        root = _find_project_root()
        assert root is not None
        assert (root / "pyfuse").is_dir()


class TestTempVenv:
    def test_creates_and_cleans_up(self) -> None:
        with temp_venv(install_pyfuse=False) as venv:
            assert venv.venv_dir.exists()
            assert venv.python.exists()
            venv_dir = venv.venv_dir
        # Parent tmpdir is also removed
        assert not venv_dir.exists()
        assert not venv_dir.parent.exists()

    def test_python_is_functional(self) -> None:
        with temp_venv(install_pyfuse=False) as venv:
            result = subprocess.run(
                [str(venv.python), "-c", "import sys; print(sys.prefix)"],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0
            assert str(venv.venv_dir) in result.stdout.strip()

    def test_cleanup_on_error(self) -> None:
        tmpdir_path: str | None = None
        with pytest.raises(RuntimeError, match="deliberate"):
            with temp_venv(install_pyfuse=False) as venv:
                tmpdir_path = str(venv.venv_dir.parent)
                assert venv.venv_dir.exists()
                raise RuntimeError("deliberate")
        assert tmpdir_path is not None
        assert not os.path.exists(tmpdir_path)

    def test_cleanup_on_systemexit(self) -> None:
        tmpdir_path: str | None = None
        with pytest.raises(SystemExit):
            with temp_venv(install_pyfuse=False) as venv:
                tmpdir_path = str(venv.venv_dir.parent)
                raise SystemExit(1)
        assert tmpdir_path is not None
        assert not os.path.exists(tmpdir_path)

    def test_pip_install(self) -> None:
        with temp_venv(install_pyfuse=False) as venv:
            venv.pip_install("six")
            result = subprocess.run(
                [str(venv.python), "-c", "import six; print(six.__version__)"],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0

    def test_installs_pyfuse(self) -> None:
        with temp_venv(install_pyfuse=True) as venv:
            result = subprocess.run(
                [str(venv.python), "-c", "import pyfuse; print(pyfuse.serialize)"],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0


class TestCleanupStaleVenvs:
    def test_removes_old_dirs(self) -> None:
        prefix = "pyfuse-test-stale-"
        stale = tempfile.mkdtemp(prefix=prefix)
        # Backdate mtime to 2 days ago
        old_time = time.time() - 2 * 86400
        os.utime(stale, (old_time, old_time))

        removed = cleanup_stale_venvs(prefix=prefix, max_age_secs=86400)
        assert stale in removed
        assert not os.path.exists(stale)

    def test_keeps_recent_dirs(self) -> None:
        prefix = "pyfuse-test-recent-"
        recent = tempfile.mkdtemp(prefix=prefix)
        try:
            removed = cleanup_stale_venvs(prefix=prefix, max_age_secs=86400)
            assert recent not in removed
            assert os.path.exists(recent)
        finally:
            shutil.rmtree(recent, ignore_errors=True)

    def test_ignores_non_matching_prefix(self) -> None:
        other = tempfile.mkdtemp(prefix="unrelated-")
        old_time = time.time() - 2 * 86400
        os.utime(other, (old_time, old_time))
        try:
            removed = cleanup_stale_venvs(prefix=_DEFAULT_PREFIX, max_age_secs=86400)
            assert other not in removed
            assert os.path.exists(other)
        finally:
            shutil.rmtree(other, ignore_errors=True)


class TestDetectScriptPackages:
    def test_detects_third_party(self, tmp_path: pytest.TempPathFactory) -> None:
        from pyfuse.__main__ import _detect_script_packages

        script = tmp_path / "test_script.py"  # type: ignore[operator]
        script.write_text("import requests\nimport json\nimport yaml\n")
        packages = _detect_script_packages(str(script))
        assert "requests" in packages
        assert "PyYAML" in packages
        # json is stdlib, should not appear
        assert "json" not in packages

    def test_handles_syntax_error(self, tmp_path: pytest.TempPathFactory) -> None:
        from pyfuse.__main__ import _detect_script_packages

        script = tmp_path / "bad.py"  # type: ignore[operator]
        script.write_text("def broken(:\n")
        assert _detect_script_packages(str(script)) == []

    def test_from_imports(self, tmp_path: pytest.TempPathFactory) -> None:
        from pyfuse.__main__ import _detect_script_packages

        script = tmp_path / "test_from.py"  # type: ignore[operator]
        script.write_text("from dateutil import parser\nfrom os.path import join\n")
        packages = _detect_script_packages(str(script))
        assert "python-dateutil" in packages
        # os is stdlib
        assert "os" not in packages


class TestDetectPyfuseExtras:
    def test_connect_redis(self, tmp_path: pytest.TempPathFactory) -> None:
        from pyfuse.__main__ import _detect_pyfuse_extras

        script = tmp_path / "conn.py"  # type: ignore[operator]
        script.write_text('import pyfuse\npyfuse.connect("redis://localhost:6379")\n')
        assert "redis" in _detect_pyfuse_extras(str(script))

    def test_serve_redis(self, tmp_path: pytest.TempPathFactory) -> None:
        from pyfuse.__main__ import _detect_pyfuse_extras

        script = tmp_path / "srv.py"  # type: ignore[operator]
        script.write_text('import pyfuse\npyfuse.serve("rediss://host:6380")\n')
        assert "redis" in _detect_pyfuse_extras(str(script))

    def test_bare_connect(self, tmp_path: pytest.TempPathFactory) -> None:
        from pyfuse.__main__ import _detect_pyfuse_extras

        script = tmp_path / "bare.py"  # type: ignore[operator]
        script.write_text('from pyfuse import connect\nconnect("redis://localhost")\n')
        assert "redis" in _detect_pyfuse_extras(str(script))

    def test_shm_no_redis(self, tmp_path: pytest.TempPathFactory) -> None:
        from pyfuse.__main__ import _detect_pyfuse_extras

        script = tmp_path / "shm.py"  # type: ignore[operator]
        script.write_text('import pyfuse\npyfuse.connect("shm://localhost:9847")\n')
        assert _detect_pyfuse_extras(str(script)) == []

    def test_no_connect_calls(self, tmp_path: pytest.TempPathFactory) -> None:
        from pyfuse.__main__ import _detect_pyfuse_extras

        script = tmp_path / "none.py"  # type: ignore[operator]
        script.write_text('import pyfuse\nx = 1\n')
        assert _detect_pyfuse_extras(str(script)) == []

    def test_variable_arg_ignored(self, tmp_path: pytest.TempPathFactory) -> None:
        from pyfuse.__main__ import _detect_pyfuse_extras

        script = tmp_path / "var.py"  # type: ignore[operator]
        script.write_text('import pyfuse\nurl = "redis://localhost"\npyfuse.connect(url)\n')
        assert _detect_pyfuse_extras(str(script)) == []


class TestCLIParsing:
    def test_worker_tmp_flag(self) -> None:
        """Verify --tmp is accepted by the argument parser."""
        import argparse

        from pyfuse.__main__ import main

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        worker_p = sub.add_parser("worker")
        worker_p.add_argument("--backend", default=None)
        worker_p.add_argument("--tmp", action="store_true")
        worker_p.add_argument("-c", "--concurrency", type=int, default=1)
        worker_p.add_argument("--no-auto-install", action="store_true")
        worker_p.add_argument("-v", "--verbose", action="store_true")
        worker_p.add_argument("--log-level", default=None)

        args = parser.parse_args(["worker", "--backend", "redis://localhost", "--tmp"])
        assert args.tmp is True
        assert args.backend == "redis://localhost"

    def test_run_subcommand(self) -> None:
        """Verify run subcommand parses correctly."""
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        run_p = sub.add_parser("run")
        run_p.add_argument("script")
        run_p.add_argument("-e", "--extra", action="append", default=[])
        run_p.add_argument("script_args", nargs=argparse.REMAINDER)

        args = parser.parse_args(["run", "-e", "requests", "script.py", "--", "--flag"])
        assert args.command == "run"
        assert args.script == "script.py"
        assert args.extra == ["requests"]
        assert "--flag" in args.script_args
