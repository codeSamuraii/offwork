"""CLI entrypoint for pyfuse.

Usage::

    python -m pyfuse worker --backend redis://localhost:6379
"""
from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pyfuse",
        description="pyfuse - Python Function Serializer",
    )
    sub = parser.add_subparsers(dest="command")

    worker_parser = sub.add_parser("worker", help="Start a pyfuse worker")
    worker_parser.add_argument(
        "--backend",
        required=True,
        help="Backend URL (e.g. redis://localhost:6379, shm://localhost:9847)",
    )
    worker_parser.add_argument(
        "--no-auto-install",
        action="store_true",
        help="Disable automatic pip dependency installation",
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "worker":
        from pyfuse._remote import serve

        serve(args.backend, auto_install=not args.no_auto_install)


if __name__ == "__main__":
    main()
