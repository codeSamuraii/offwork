"""CLI entrypoint for pyfuse.

Usage::

    python -m pyfuse worker --backend redis://localhost:6379
    python -m pyfuse worker --backend redis://localhost:6379 --tmp
    python -m pyfuse run examples/script.py
    python -m pyfuse info
    python -m pyfuse serialize mymodule:csv_to_json
    python -m pyfuse reconstruct graph.json csv_to_json
"""
from __future__ import annotations

import argparse
import ast
import importlib
import logging
import os
import signal
import subprocess
import sys
from collections.abc import Callable


def _build_worker_cmd(python: str, args: argparse.Namespace) -> list[str]:
    """Rebuild the worker CLI command for re-exec, without --tmp."""
    cmd = [python, "-m", "pyfuse", "worker", "--backend", args.backend]
    cmd.extend(["-c", str(args.concurrency)])
    if args.no_auto_install:
        cmd.append("--no-auto-install")
    if args.verbose:
        cmd.append("--verbose")
    if args.log_level:
        cmd.extend(["--log-level", args.log_level])
    return cmd


def _run_in_tmp_venv(args: argparse.Namespace) -> None:
    """Create a temporary venv and re-exec the worker inside it."""
    from pyfuse._venv import temp_venv

    extras: list[str] = []
    if args.backend and args.backend.startswith(("redis://", "rediss://")):
        extras.append("redis")

    with temp_venv(install_pyfuse=True, extras=extras) as venv:
        cmd = _build_worker_cmd(str(venv.python), args)
        print(f"Starting worker in temporary venv...", file=sys.stderr)
        proc = subprocess.Popen(cmd)

        prev_term = signal.signal(signal.SIGTERM, lambda s, f: proc.send_signal(s))
        prev_int = signal.signal(signal.SIGINT, lambda s, f: proc.send_signal(s))
        try:
            returncode = proc.wait()
        finally:
            signal.signal(signal.SIGTERM, prev_term)
            signal.signal(signal.SIGINT, prev_int)

        sys.exit(returncode)


def _cmd_worker(args: argparse.Namespace) -> None:
    backend = args.backend
    if not backend:
        print("Error: --backend is required (or set PYFUSE_BACKEND).", file=sys.stderr)
        sys.exit(1)

    if args.tmp:
        _run_in_tmp_venv(args)
        return

    if args.log_level:
        level = getattr(logging, args.log_level.upper(), None)
        if level is None:
            print(f"Error: invalid log level {args.log_level!r}", file=sys.stderr)
            sys.exit(1)
    elif args.verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    ))
    pyfuse_logger = logging.getLogger("pyfuse")
    pyfuse_logger.setLevel(level)
    pyfuse_logger.addHandler(handler)

    from pyfuse.worker.remote import serve

    serve(backend, concurrency=args.concurrency, auto_install=not args.no_auto_install)


def _cmd_info(_args: argparse.Namespace) -> None:
    from importlib.metadata import version as pkg_version

    try:
        ver = pkg_version("pyfuse")
    except Exception:
        ver = "unknown"

    print(f"pyfuse {ver}")
    print(f"  PYFUSE_BACKEND = {os.environ.get('PYFUSE_BACKEND', '(not set)')}")

    for dep in ("redis",):
        try:
            dep_ver = pkg_version(dep)
            print(f"  {dep}: {dep_ver}")
        except Exception:
            print(f"  {dep}: not installed")


def _import_target(target: str) -> Callable[..., object]:
    """Import ``module:function`` and return the callable."""
    if ":" not in target:
        print(
            f"Error: target must be 'module:function', got {target!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    module_path, func_name = target.rsplit(":", 1)
    mod = importlib.import_module(module_path)
    func: Callable[..., object] | None = getattr(mod, func_name, None)
    if func is None:
        print(
            f"Error: {func_name!r} not found in module {module_path!r}",
            file=sys.stderr,
        )
        sys.exit(1)
    return func


def _cmd_serialize(args: argparse.Namespace) -> None:
    from pyfuse import serialize

    func = _import_target(args.target)
    print(serialize(func))


def _cmd_reconstruct(args: argparse.Namespace) -> None:
    from pathlib import Path

    from pyfuse import reconstruct

    graph_json = Path(args.graph_file).read_text()
    print(reconstruct(graph_json, args.function))


def _parse_script(script: str) -> tuple[str, ast.Module | None]:
    """Read and parse a script, returning (source, ast_tree_or_None)."""
    from pathlib import Path

    source = Path(script).read_text()
    try:
        return source, ast.parse(source)
    except SyntaxError:
        return source, None


def _is_local_package(module_name: str, script_dir: str) -> bool:
    """Return True if *module_name* resolves to a local directory or .py file."""
    from pathlib import Path

    for base in (Path(script_dir), Path.cwd()):
        if (base / module_name).is_dir():
            return True
        if (base / f"{module_name}.py").is_file():
            return True
    return False


def _parse_install_package_as(node: ast.With) -> str | None:
    """Return the package name if *node* is ``with install_package_as(...)``."""
    if len(node.items) != 1:
        return None
    ctx = node.items[0].context_expr
    if not (
        isinstance(ctx, ast.Call)
        and isinstance(ctx.func, ast.Name)
        and ctx.func.id == "install_package_as"
        and len(ctx.args) == 1
        and isinstance(ctx.args[0], ast.Constant)
        and isinstance(ctx.args[0].value, str)
    ):
        return None
    return ctx.args[0].value


def _extract_top_modules(node: ast.AST) -> list[str]:
    """Extract top-level module names from an Import or ImportFrom node."""
    if isinstance(node, ast.Import):
        return [alias.name.split(".")[0] for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
        return [node.module.split(".")[0]]
    return []


def _detect_script_packages(script: str) -> list[str]:
    """Parse a script file and return pip package names for third-party imports."""
    from pathlib import Path

    from pyfuse.worker.deps import DEFAULT_IMPORT_TO_PACKAGE

    _source, tree = _parse_script(script)
    if tree is None:
        return []

    script_dir = str(Path(script).resolve().parent)

    # module name -> pip package name (None means use default mapping)
    modules: dict[str, str | None] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for m in _extract_top_modules(node):
                modules.setdefault(m, None)
        elif isinstance(node, ast.With):
            package = _parse_install_package_as(node)
            if package is not None:
                for child in node.body:
                    for m in _extract_top_modules(child):
                        modules[m] = package

    packages: dict[str, None] = {}
    for m, explicit_package in sorted(modules.items()):
        if m in sys.stdlib_module_names or m == "pyfuse":
            continue
        if _is_local_package(m, script_dir):
            continue
        pkg = explicit_package or DEFAULT_IMPORT_TO_PACKAGE.get(m, m)
        packages.setdefault(pkg, None)
    return list(packages)


def _detect_pyfuse_extras(script: str) -> list[str]:
    """Detect pyfuse extras needed by a script (e.g. redis from connect/serve calls)."""
    _source, tree = _parse_script(script)
    if tree is None:
        return []

    extras: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        # Match pyfuse.connect(...), pyfuse.serve(...), connect(...), serve(...)
        func = node.func
        name: str | None = None
        if isinstance(func, ast.Attribute) and func.attr in ("connect", "serve"):
            name = func.attr
        elif isinstance(func, ast.Name) and func.id in ("connect", "serve"):
            name = func.id
        if name is None:
            continue
        # Check if the first argument is a string literal with a redis URL
        first_arg = node.args[0]
        if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
            if first_arg.value.startswith(("redis://", "rediss://")):
                if "redis" not in extras:
                    extras.append("redis")

    return extras


def _cmd_run(args: argparse.Namespace) -> None:
    from pathlib import Path

    from pyfuse._venv import temp_venv

    script = Path(args.script).resolve()
    if not script.exists():
        print(f"Error: script not found: {script}", file=sys.stderr)
        sys.exit(1)

    extras: list[str] = list(args.extra or [])
    backend = os.environ.get("PYFUSE_BACKEND", "")
    if backend.startswith(("redis://", "rediss://")):
        if "redis" not in extras:
            extras.append("redis")

    # Auto-detect pyfuse extras from connect/serve calls in the script
    for extra in _detect_pyfuse_extras(str(script)):
        if extra not in extras:
            extras.append(extra)

    # Auto-detect third-party packages from the script
    detected = _detect_script_packages(str(script))

    with temp_venv(install_pyfuse=True, extras=extras) as venv:
        if detected:
            print(f"Installing detected dependencies: {', '.join(detected)}", file=sys.stderr)
            venv.pip_install(*detected, extra_args=["--quiet"])

        script_args = list(args.script_args or [])
        if script_args and script_args[0] == "--":
            script_args = script_args[1:]

        # Add cwd to PYTHONPATH so local packages (tests/, etc.) are importable
        env = os.environ.copy()
        cwd = os.getcwd()
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = cwd if not existing else f"{cwd}{os.pathsep}{existing}"

        cmd = [str(venv.python), str(script), *script_args]
        proc = subprocess.Popen(cmd, env=env)

        prev_term = signal.signal(signal.SIGTERM, lambda s, f: proc.send_signal(s))
        prev_int = signal.signal(signal.SIGINT, lambda s, f: proc.send_signal(s))
        try:
            returncode = proc.wait()
        finally:
            signal.signal(signal.SIGTERM, prev_term)
            signal.signal(signal.SIGINT, prev_int)

        sys.exit(returncode)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pyfuse",
        description="pyfuse - distributed task execution",
    )
    sub = parser.add_subparsers(dest="command")

    # worker
    worker_p = sub.add_parser("worker", help="Start a pyfuse worker")
    worker_p.add_argument(
        "--backend",
        default=os.environ.get("PYFUSE_BACKEND"),
        help="Backend URL, e.g. redis://localhost:6379 (default: $PYFUSE_BACKEND)",
    )
    worker_p.add_argument(
        "-c", "--concurrency",
        type=int,
        default=1,
        help="Number of concurrent worker threads (default: 1)",
    )
    worker_p.add_argument(
        "--no-auto-install",
        action="store_true",
        help="Disable automatic pip dependency installation",
    )
    worker_p.add_argument(
        "--tmp",
        action="store_true",
        help="Run the worker in a temporary virtual environment (deleted on exit)",
    )
    worker_p.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    worker_p.add_argument(
        "--log-level",
        default=None,
        metavar="LEVEL",
        help="Set log level (DEBUG, INFO, WARNING, ERROR)",
    )

    # run
    run_p = sub.add_parser(
        "run", help="Run a Python script in a temporary venv with pyfuse installed"
    )
    run_p.add_argument("script", help="Path to the Python script to run")
    run_p.add_argument(
        "-e", "--extra",
        action="append",
        default=[],
        help="Extra pip package to install (repeatable)",
    )
    run_p.add_argument(
        "script_args",
        nargs=argparse.REMAINDER,
        help="Arguments to pass to the script",
    )

    # info
    sub.add_parser("info", help="Show pyfuse configuration")

    # serialize
    ser_p = sub.add_parser("serialize", help="Serialize a function to JSON")
    ser_p.add_argument("target", help="module:function to serialize")

    # reconstruct
    rec_p = sub.add_parser("reconstruct", help="Reconstruct source from graph JSON")
    rec_p.add_argument("graph_file", help="Path to graph JSON file")
    rec_p.add_argument("function", help="Function name to reconstruct")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    handlers = {
        "worker": _cmd_worker,
        "run": _cmd_run,
        "info": _cmd_info,
        "serialize": _cmd_serialize,
        "reconstruct": _cmd_reconstruct,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
