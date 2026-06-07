"""Temporary virtual environment management for offwork."""

import os
import sys
import time
import venv
import atexit
import shutil
import signal
import asyncio
import logging
import tempfile
from types import FrameType
from pathlib import Path
from contextlib import asynccontextmanager
from dataclasses import dataclass
from collections.abc import Sequence, AsyncIterator

logger = logging.getLogger(__name__)

_STALE_THRESHOLD_SECS = 60 * 60 * 24  # 24 hours
_DEFAULT_PREFIX = "offwork-tmp-"


def _find_project_root() -> Path | None:
    """Walk up from this file to find the directory containing pyproject.toml."""
    current = Path(__file__).resolve().parent
    while current != current.parent:
        if (current / "pyproject.toml").exists():
            return current
        current = current.parent
    return None


def _venv_python(venv_dir: Path) -> Path:
    """Return the path to the Python executable inside a venv."""
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def cleanup_stale_venvs(
    prefix: str = _DEFAULT_PREFIX,
    max_age_secs: float = _STALE_THRESHOLD_SECS,
) -> list[str]:
    """Remove stale offwork temp directories left behind by crashed processes.

    Returns the list of directories that were removed.
    """
    tmproot = Path(tempfile.gettempdir())
    cutoff = time.time() - max_age_secs
    removed: list[str] = []

    for entry in tmproot.iterdir():
        if not entry.name.startswith(prefix) or not entry.is_dir():
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            logger.info("Removing stale temp venv: %s", entry)
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(str(entry))

    return removed


@dataclass
class TempVenv:
    """A temporary virtual environment."""

    venv_dir: Path
    python: Path

    async def pip_install(
        self, *packages: str, extra_args: Sequence[str] = ()
    ) -> None:
        """Install packages via pip in this venv."""
        if not packages:
            return
        cmd = [str(self.python), "-m", "pip", "install", *packages, *extra_args]
        logger.info("Installing in temp venv: %s", " ".join(packages))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"pip install failed in temp venv:\n{stderr.decode() if stderr else ''}"
            )


@asynccontextmanager
async def temp_venv(
    *,
    install_offwork: bool = True,
    extras: Sequence[str] = (),
    prefix: str = _DEFAULT_PREFIX,
) -> AsyncIterator[TempVenv]:
    """Create a temporary venv, optionally install offwork, yield, then clean up.

    Cleanup is guaranteed on normal exit, exceptions, SIGTERM, SIGINT, and
    ``atexit``.  Stale directories from previously crashed processes are
    cleaned up on entry.

    Parameters
    ----------
    install_offwork:
        Install offwork into the venv (editable from source tree, or from PyPI).
    extras:
        Optional extras to install, e.g. ``("redis",)``.
    prefix:
        Prefix for the temporary directory name.
    """
    # Opportunistically clean up leftovers from previous crashes.
    cleanup_stale_venvs(prefix)

    tmpdir = tempfile.mkdtemp(prefix=prefix)

    def _cleanup() -> None:
        if os.path.isdir(tmpdir):
            logger.info("Cleaning up temporary venv at %s", tmpdir)
            shutil.rmtree(tmpdir, ignore_errors=True)

    # Safety net: atexit ensures cleanup even if the context manager is
    # bypassed (e.g. an unhandled exception in calling code outside the
    # ``with`` block, or ``sys.exit()`` called elsewhere).
    atexit.register(_cleanup)

    # SIGTERM normally kills the process before context-manager __exit__
    # runs.  Convert it into a clean SystemExit so the finally block and
    # atexit handlers execute.
    prev_sigterm = signal.getsignal(signal.SIGTERM)

    def _sigterm_handler(signum: int, frame: FrameType | None) -> None:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _sigterm_handler)

    try:
        venv_dir = Path(tmpdir) / "venv"
        logger.info("Creating temporary venv: %s", venv_dir)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, lambda: venv.create(str(venv_dir), with_pip=True)
        )

        python = _venv_python(venv_dir)
        if not python.exists():
            raise RuntimeError(f"venv Python not found at {python}")

        tv = TempVenv(venv_dir=venv_dir, python=python)

        if install_offwork:
            root = _find_project_root()
            if root is not None:
                spec = str(root)
            else:
                spec = "offwork"
            if extras:
                spec += f"[{','.join(extras)}]"
            await tv.pip_install(spec, extra_args=["--quiet"])

        yield tv
    finally:
        _cleanup()
        atexit.unregister(_cleanup)
        signal.signal(signal.SIGTERM, prev_sigterm)
