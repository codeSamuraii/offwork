"""CLI entrypoint for pyfuse.

Usage::

    python -m pyfuse worker --backend redis://localhost:6379
    python -m pyfuse info
    python -m pyfuse serialize mymodule:csv_to_json
    python -m pyfuse reconstruct graph.json csv_to_json
"""
from __future__ import annotations

import argparse
import importlib
import os
import sys


def _cmd_worker(args: argparse.Namespace) -> None:
    backend = args.backend
    if not backend:
        print("Error: --backend is required (or set PYFUSE_BACKEND).", file=sys.stderr)
        sys.exit(1)

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


def _import_target(target: str) -> object:
    """Import ``module:function`` and return the callable."""
    if ":" not in target:
        print(
            f"Error: target must be 'module:function', got {target!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    module_path, func_name = target.rsplit(":", 1)
    mod = importlib.import_module(module_path)
    func = getattr(mod, func_name, None)
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
        "info": _cmd_info,
        "serialize": _cmd_serialize,
        "reconstruct": _cmd_reconstruct,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
