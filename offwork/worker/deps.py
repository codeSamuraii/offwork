"""Third-party dependency extraction and pip installation."""

import ast
import sys
import types
import inspect
import asyncio
import logging
import contextlib
import importlib.abc
import importlib.util
import importlib.machinery
from typing import Any
from pathlib import Path
from dataclasses import field, dataclass
from collections.abc import Iterator

from offwork.core.errors import DependencyError, WorkerOnlyError
from offwork.graph.store import Store

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
    ``@offwork.task`` analyzer detects the ``with`` block in the AST and records
    the *package* name on every :class:`ImportInfo` inside it, so the
    worker knows which pip package to install.

    Example::

        with install_package_as('opencv-python'):
            import cv2
    """
    yield


# ---------------------------------------------------------------------------
# Worker-only imports: stub the package on the client, install on the worker
# ---------------------------------------------------------------------------


class _WorkerOnlyStub(types.ModuleType):
    """Module subclass that raises ``WorkerOnlyError`` on any real use."""

    __offwork_stub__ = True

    def _err(self) -> WorkerOnlyError:
        return WorkerOnlyError(
            f"'{self.__name__}' was imported with worker_only_import; "
            "it must only be used inside a @offwork.task function executed on a worker"
        )

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        attr = _StubAttr(f"{self.__name__}.{name}")
        setattr(self, name, attr)
        return attr


class _StubAttr:
    """Placeholder returned for any attribute access on a stub module."""

    __offwork_stub__ = True

    def __init__(self, qualname: str) -> None:
        self._qualname = qualname

    def _err(self) -> WorkerOnlyError:
        return WorkerOnlyError(
            f"'{self._qualname}' was imported with worker_only_import; "
            "it must only be used inside a @offwork.task function executed on a worker"
        )

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise self._err()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return _StubAttr(f"{self._qualname}.{name}")


class _WorkerOnlyFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Meta-path finder that fabricates stubs for an explicit module whitelist.

    Inserted at the *end* of ``sys.meta_path`` so real installed packages
    still win.  Only stubs modules whose top-level name appears in the
    whitelist (built from the caller's ``with`` block source), so missing
    transitive imports from real installed packages still raise the normal
    ``ModuleNotFoundError`` instead of being silently stubbed.
    """

    def __init__(self) -> None:
        self.allowed: set[str] = set()

    def find_spec(
        self,
        fullname: str,
        path: Any = None,
        target: types.ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        top = fullname.split(".", 1)[0]
        if top not in self.allowed:
            return None
        return importlib.machinery.ModuleSpec(fullname, self, is_package=True)

    def create_module(
        self, spec: importlib.machinery.ModuleSpec
    ) -> types.ModuleType | None:
        module = _WorkerOnlyStub(spec.name)
        module.__path__ = []  # mark as package so submodule imports work
        return module

    def exec_module(self, module: types.ModuleType) -> None:
        return None


_worker_only_finder: _WorkerOnlyFinder | None = None
_worker_only_depth: int = 0


def _get_worker_only_finder() -> _WorkerOnlyFinder:
    global _worker_only_finder
    if _worker_only_finder is None:
        _worker_only_finder = _WorkerOnlyFinder()
    return _worker_only_finder


def _module_names_in_with_block(filename: str, lineno: int) -> set[str]:
    """Return top-level module names imported inside the ``with`` block at *lineno*.

    Parses *filename* and finds the ``With`` AST node starting at *lineno*,
    then collects the top-level module name of each ``Import`` /
    ``ImportFrom`` statement in its body.
    """
    try:
        source = Path(filename).read_text()
    except OSError:
        return set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    target: ast.With | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.With) and node.lineno == lineno:
            target = node
            break
    if target is None:
        return set()

    names: set[str] = set()
    for child in ast.walk(target):
        if isinstance(child, ast.Import):
            for alias in child.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(child, ast.ImportFrom) and child.module and child.level == 0:
            names.add(child.module.split(".")[0])
    return names


@contextlib.contextmanager
def worker_only_import(package: str | None = None) -> Iterator[None]:
    """Skip installing packages locally; the worker installs them on demand.

    Imports inside this block resolve to lightweight stubs on the client.
    The ``@offwork.task`` analyzer records the imports as worker-only, and the
    worker installs them via pip before reconstructing the function.

    The optional *package* argument overrides the pip package name (same
    semantics as :func:`install_package_as`).

    Example::

        with offwork.worker_only_import():
            import requests

        with offwork.worker_only_import("opencv-python-headless"):
            import cv2

    Stubs raise :class:`WorkerOnlyError` if used outside a worker context.
    """
    del package  # consumed by the AST analyzer, not at runtime
    global _worker_only_depth

    # Determine the whitelist by parsing the caller's with-block source.
    frame = inspect.currentframe()
    caller = frame.f_back.f_back if frame and frame.f_back else None
    new_allowed: set[str] = set()
    if caller is not None:
        new_allowed = _module_names_in_with_block(
            caller.f_code.co_filename, caller.f_lineno,
        )

    finder = _get_worker_only_finder()
    previous_allowed = set(finder.allowed)
    finder.allowed |= new_allowed
    if finder not in sys.meta_path:
        sys.meta_path.append(finder)
    _worker_only_depth += 1
    try:
        yield
    finally:
        _worker_only_depth -= 1
        # Restore the allowed set so nested blocks behave like a stack.
        finder.allowed = previous_allowed
        if _worker_only_depth == 0 and finder in sys.meta_path:
            sys.meta_path.remove(finder)


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
    # returncode is None if the process hasn't terminated; treat as failure
    returncode = proc.returncode if proc.returncode is not None else 1
    return (
        returncode,
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
    raise DependencyError("Failed to install:\n" + "\n".join(parts))


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

    if result.installed:
        # Drop importlib's cached "this directory doesn't exist / has no such
        # module" entries so the next ``find_spec``/``import`` picks up files
        # pip just wrote. Required when the target site-packages directory
        # was created at install time (e.g. an emptyDir volume mounted over
        # an empty user-site).
        importlib.invalidate_caches()

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
