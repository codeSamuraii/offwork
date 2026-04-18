"""Third-party dependency extraction and pip installation."""

import ast
import sys
import asyncio
import logging
import contextlib
import importlib.util
from typing import Any
from dataclasses import field, dataclass
from collections.abc import Iterator

from pyfuse.core.errors import DependencyError
from pyfuse.graph.store import Store

logger = logging.getLogger(__name__)

DEFAULT_IMPORT_TO_PACKAGE: dict[str, str] = {
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "sklearn": "scikit-learn",
    "bs4": "beautifulsoup4",
    "yaml": "PyYAML",
    "attr": "attrs",
    "dateutil": "python-dateutil",
    "gi": "PyGObject",
    "Crypto": "pycryptodome",
    "serial": "pyserial",
}


@contextlib.contextmanager
def install_package_as(package: str) -> Iterator[None]:
    """Declare the pip package name for imports inside this block.

    At runtime this is a no-op -- the import executes normally.  The
    ``@trace`` analyzer detects the ``with`` block in the AST and records
    the *package* name on every :class:`ImportInfo` inside it, so the
    worker knows which pip package to install.

    Example::

        with install_package_as('opencv-python'):
            import cv2
    """
    yield


@dataclass
class InstallResult:
    """Outcome of dependency installation."""

    installed: list[str] = field(default_factory=list)
    already_present: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


def _extract_top_module(statement: str) -> str:
    """Parse an import statement and return the top-level module name."""
    tree = ast.parse(statement)
    node = tree.body[0]
    if isinstance(node, ast.Import):
        return node.names[0].name.split(".")[0]
    if isinstance(node, ast.ImportFrom) and node.module:
        return node.module.split(".")[0]
    raise ValueError(f"Cannot extract module from: {statement}")


def extract_third_party_modules(
    store: Store, function_name: str
) -> set[str]:
    """Return third-party module names needed by *function_name* and its deps."""
    _target_qname, nodes = store.collect(function_name)
    modules: set[str] = set()
    for node in nodes.values():
        for imp in node.imports:
            try:
                top = _extract_top_module(imp.statement)
            except (ValueError, IndexError):
                continue
            if top and top not in sys.stdlib_module_names:
                modules.add(top)
    return modules


def is_installed(module: str) -> bool:
    """Check whether *module* is importable."""
    return importlib.util.find_spec(module) is not None


async def _pip_install(package: str, extra_args: list[str]) -> tuple[int, str, str]:
    """Run ``pip install <package>`` and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", "install", package, *extra_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout_bytes.decode() if stdout_bytes else "",
        stderr_bytes.decode() if stderr_bytes else "",
    )


def _raise_on_failures(failed: dict[str, str]) -> None:
    """Raise :class:`DependencyError` if any installations failed."""
    if not failed:
        return
    parts = [
        f"  {module}: {stderr[:200]}" if stderr else f"  {module}"
        for module, stderr in failed.items()
    ]
    raise DependencyError(f"Failed to install:\n" + "\n".join(parts))


async def install_packages(
    modules: set[str],
    import_to_package: dict[str, str] | None = None,
    pip_args: list[str] | None = None,
) -> InstallResult:
    """Install missing packages via pip. Returns an :class:`InstallResult`.

    Raises :class:`DependencyError` if any installation fails.
    """
    mapping = {**DEFAULT_IMPORT_TO_PACKAGE, **(import_to_package or {})}
    extra_args = pip_args or []
    result = InstallResult()

    for module in sorted(modules):
        if is_installed(module):
            result.already_present.append(module)
            continue

        package = mapping.get(module, module)
        logger.debug("pip install %s ...", package)
        returncode, _stdout, stderr = await _pip_install(package, extra_args)
        if returncode == 0:
            result.installed.append(package)
            logger.debug("Installed %s", package)
        else:
            result.failed[module] = stderr.strip()
            logger.error("Failed to install %s: %s", package, stderr.strip()[:200])

    _raise_on_failures(result.failed)
    return result


def _collect_package_hints(
    store: Store, function_name: str
) -> dict[str, str]:
    """Extract import->package mappings from ``ImportInfo.package`` fields."""
    _target_qname, nodes = store.collect(function_name)
    hints: dict[str, str] = {}
    for node in nodes.values():
        for imp in node.imports:
            if imp.package:
                try:
                    top = _extract_top_module(imp.statement)
                except (ValueError, IndexError):
                    continue
                hints[top] = imp.package
    return hints


async def ensure_dependencies(
    store: Store,
    function_name: str,
    import_to_package: dict[str, str] | None = None,
) -> InstallResult:
    """Extract third-party modules and install any that are missing.

    Package hints from ``install_package_as`` blocks in the serialized graph
    are automatically used.  Explicit *import_to_package* entries take
    priority over hints, which take priority over ``DEFAULT_IMPORT_TO_PACKAGE``.
    """
    modules = extract_third_party_modules(store, function_name)
    if not modules:
        return InstallResult()
    hints = _collect_package_hints(store, function_name)
    merged = {**hints, **(import_to_package or {})}
    return await install_packages(modules, merged)
