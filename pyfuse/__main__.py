"""CLI entrypoint for pyfuse.

Usage::

    pyfuse worker --backend redis://localhost:6379
    pyfuse worker --backend redis://localhost:6379 --tmp
    pyfuse run examples/script.py
    pyfuse info
    pyfuse serialize mymodule:csv_to_json
    pyfuse reconstruct graph.json csv_to_json
"""
import argparse
import ast
import asyncio
import importlib
import logging
import os
import signal
import sys
from collections.abc import Callable
from importlib.metadata import version as pkg_version
from pathlib import Path

from pyfuse import reconstruct, serialize
from pyfuse._venv import temp_venv
from pyfuse.worker.deps import DEFAULT_IMPORT_TO_PACKAGE
from pyfuse.worker.remote import serve


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


async def _run_in_tmp_venv(args: argparse.Namespace) -> None:
    """Create a temporary venv and re-exec the worker inside it."""
    extras: list[str] = []
    if args.backend and args.backend.startswith(("redis://", "rediss://")):
        extras.append("redis")

    async with temp_venv(install_pyfuse=True, extras=extras) as venv:
        cmd = _build_worker_cmd(str(venv.python), args)
        print("Starting worker in temporary venv...", file=sys.stderr)
        await _run_subprocess_async(cmd)


def _resolve_log_level(args: argparse.Namespace) -> int:
    """Determine the log level from CLI arguments."""
    if args.log_level:
        level = getattr(logging, args.log_level.upper(), None)
        if level is None:
            print(f"Error: invalid log level {args.log_level!r}", file=sys.stderr)
            sys.exit(1)
        return level  # type: ignore[no-any-return]
    if args.verbose:
        return logging.DEBUG
    return logging.INFO


def _configure_logging(level: int) -> None:
    """Set up pyfuse logger with a stderr handler."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
    ))
    pyfuse_logger = logging.getLogger("pyfuse")
    pyfuse_logger.setLevel(level)
    pyfuse_logger.addHandler(handler)


def _cmd_worker(args: argparse.Namespace) -> None:
    if not args.backend:
        print("Error: --backend is required (or set PYFUSE_BACKEND).", file=sys.stderr)
        sys.exit(1)

    if args.tmp:
        asyncio.run(_run_in_tmp_venv(args))
        return

    _configure_logging(_resolve_log_level(args))

    trust_store = None
    trust_dir = getattr(args, "trusted_keys", None) or os.environ.get("PYFUSE_TRUSTED_KEYS")
    if trust_dir:
        from pyfuse.core.signing import TrustStore

        trust_store = TrustStore.from_directory(trust_dir)
        logging.getLogger("pyfuse").info(
            "Loaded %d trusted key(s) from %s", len(trust_store), trust_dir,
        )

    asyncio.run(serve(
        args.backend,
        concurrency=args.concurrency,
        auto_install=not args.no_auto_install,
        sandbox=bool(args.sandbox),
        trust_store=trust_store,
    ))


def _cmd_info(_args: argparse.Namespace) -> None:
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
    func = _import_target(args.target)
    print(serialize(func))


def _cmd_reconstruct(args: argparse.Namespace) -> None:
    graph_json = Path(args.graph_file).read_text()
    print(reconstruct(graph_json, args.function))


def _parse_script(script: str) -> tuple[str, ast.Module | None]:
    """Read and parse a script, returning (source, ast_tree_or_None)."""
    source = Path(script).read_text()
    try:
        return source, ast.parse(source)
    except SyntaxError:
        return source, None


def _is_local_package(module_name: str, script_dir: str) -> bool:
    """Return True if *module_name* resolves to a local directory or .py file."""
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


def _is_connect_or_serve_call(node: ast.Call) -> bool:
    """Return True if *node* calls ``connect`` or ``serve``."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr in ("connect", "serve")
    if isinstance(func, ast.Name):
        return func.id in ("connect", "serve")
    return False


def _first_arg_is_redis_url(node: ast.Call) -> bool:
    """Return True if the first positional arg is a redis:// string literal."""
    if not node.args:
        return False
    first_arg = node.args[0]
    return (
        isinstance(first_arg, ast.Constant)
        and isinstance(first_arg.value, str)
        and first_arg.value.startswith(("redis://", "rediss://"))
    )


def _detect_pyfuse_extras(script: str) -> list[str]:
    """Detect pyfuse extras needed by a script (e.g. redis from connect/serve calls)."""
    _source, tree = _parse_script(script)
    if tree is None:
        return []

    extras: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _is_connect_or_serve_call(node) and _first_arg_is_redis_url(node):
            if "redis" not in extras:
                extras.append("redis")

    return extras


def _collect_extras(args: argparse.Namespace, script: Path) -> list[str]:
    """Gather pyfuse extras and third-party packages for a script."""
    extras: list[str] = list(args.extra or [])
    backend = os.environ.get("PYFUSE_BACKEND", "")
    if backend.startswith(("redis://", "rediss://")) and "redis" not in extras:
        extras.append("redis")
    for extra in _detect_pyfuse_extras(str(script)):
        if extra not in extras:
            extras.append(extra)
    return extras


def _build_script_env() -> dict[str, str]:
    """Build env dict with cwd prepended to PYTHONPATH."""
    env = os.environ.copy()
    cwd = os.getcwd()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = cwd if not existing else f"{cwd}{os.pathsep}{existing}"
    return env


async def _run_subprocess_async(
    cmd: list[str], env: dict[str, str] | None = None
) -> None:
    """Run a subprocess, forwarding signals and exiting with its return code."""
    proc = await asyncio.create_subprocess_exec(*cmd, env=env)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, proc.send_signal, sig)
        except (NotImplementedError, RuntimeError):
            pass  # Windows or no running loop

    returncode = await proc.wait()
    sys.exit(returncode)


async def _cmd_run_async(args: argparse.Namespace) -> None:
    script = Path(args.script).resolve()
    if not script.exists():
        print(f"Error: script not found: {script}", file=sys.stderr)
        sys.exit(1)

    extras = _collect_extras(args, script)
    detected = _detect_script_packages(str(script))

    async with temp_venv(install_pyfuse=True, extras=extras) as venv:
        if detected:
            print(f"Installing detected dependencies: {', '.join(detected)}", file=sys.stderr)
            await venv.pip_install(*detected, extra_args=["--quiet"])

        script_args = list(args.script_args or [])
        if script_args and script_args[0] == "--":
            script_args = script_args[1:]

        cmd = [str(venv.python), str(script), *script_args]
        await _run_subprocess_async(cmd, env=_build_script_env())


def _cmd_run(args: argparse.Namespace) -> None:
    asyncio.run(_cmd_run_async(args))


def _add_worker_parser(sub: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    p = sub.add_parser("worker", help="Start a pyfuse worker")
    p.add_argument(
        "--backend",
        default=os.environ.get("PYFUSE_BACKEND"),
        help="Backend URL, e.g. redis://localhost:6379 (default: $PYFUSE_BACKEND)",
    )
    p.add_argument(
        "-c", "--concurrency", type=int, default=1,
        help="Number of concurrent worker tasks (default: 1)",
    )
    p.add_argument("--no-auto-install", action="store_true",
                    help="Disable automatic pip dependency installation")
    p.add_argument("--tmp", action="store_true",
                    help="Run the worker in a temporary virtual environment (deleted on exit)")
    p.add_argument("--sandbox", action="store_true", default=False,
                    help="Run function execution inside an isolated Docker sandbox.")
    p.add_argument("--trusted-keys", default=None, metavar="DIR",
                    help="Directory of trusted .pub key files (enables signature verification). "
                         "Also settable via PYFUSE_TRUSTED_KEYS env var.")
    p.add_argument("-v", "--verbose", action="store_true",
                    help="Enable debug logging")
    p.add_argument("--log-level", default=None, metavar="LEVEL",
                    help="Set log level (DEBUG, INFO, WARNING, ERROR)")


def _add_run_parser(sub: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    p = sub.add_parser(
        "run", help="Run a Python script in a temporary venv with pyfuse installed",
    )
    p.add_argument("script", help="Path to the Python script to run")
    p.add_argument("-e", "--extra", action="append", default=[],
                    help="Extra pip package to install (repeatable)")
    p.add_argument("script_args", nargs=argparse.REMAINDER,
                    help="Arguments to pass to the script")


def _add_serialize_parser(sub: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    p = sub.add_parser("serialize", help="Serialize a function to JSON")
    p.add_argument("target", help="module:function to serialize")


def _add_reconstruct_parser(sub: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    p = sub.add_parser("reconstruct", help="Reconstruct source from graph JSON")
    p.add_argument("graph_file", help="Path to graph JSON file")
    p.add_argument("function", help="Function name to reconstruct")


def _cmd_sandbox(args: argparse.Namespace) -> None:
    """Handle ``pyfuse sandbox setup|status|teardown`` subcommands."""
    action = getattr(args, "sandbox_action", None)
    if action is None:
        print("Usage: pyfuse sandbox {setup|status|teardown}", file=sys.stderr)
        sys.exit(1)

    if action == "setup":
        asyncio.run(_docker_setup())
    elif action == "status":
        asyncio.run(_sandbox_status())
    elif action == "teardown":
        asyncio.run(_docker_teardown())


async def _sandbox_status() -> None:
    """Print the current Docker sandbox status."""
    import shutil

    if shutil.which("docker") is not None:
        from pyfuse.worker.sandbox.docker import (
            _container_exists, _container_running, _image_exists,
        )
        image = os.environ.get("PYFUSE_SANDBOX_DOCKER_IMAGE", "pyfuse-sandbox")
        container = os.environ.get("PYFUSE_SANDBOX_DOCKER_CONTAINER", "pyfuse-sandbox")
        img_ok = await _image_exists(image)
        print("Docker:")
        print(f"  docker: installed")
        print(f"  Image '{image}': {'exists' if img_ok else 'not found'}")
        if await _container_exists(container):
            running = await _container_running(container)
            print(f"  Container '{container}': {'running' if running else 'stopped'}")
        else:
            print(f"  Container '{container}': not found")
        if not img_ok:
            print("  Hint: run 'pyfuse sandbox setup' to build the image")
    else:
        print("Docker: not installed")


async def _docker_setup() -> None:
    """Build the Docker sandbox image."""
    import shutil
    if shutil.which("docker") is None:
        print(
            "Error: 'docker' command not found.\n"
            "Install Docker from https://docs.docker.com/get-docker/",
            file=sys.stderr,
        )
        sys.exit(1)

    from pyfuse.worker.sandbox.docker import _build_image, _image_exists
    image = os.environ.get("PYFUSE_SANDBOX_DOCKER_IMAGE", "pyfuse-sandbox")
    if await _image_exists(image):
        print(f"Image '{image}' already exists. Rebuilding...")
    else:
        print(f"Building image '{image}'...")
    await _build_image(image)
    print(f"Done. Start a sandboxed worker with:")
    print(f"  pyfuse worker --backend redis://localhost:6379 --sandbox")


async def _docker_teardown() -> None:
    """Stop and remove the Docker sandbox container and image."""
    import shutil
    if shutil.which("docker") is None:
        print("Docker is not installed, nothing to tear down.", file=sys.stderr)
        return

    from pyfuse.worker.sandbox.docker import (
        _container_exists, _container_running, _docker_wait, _image_exists,
    )
    container = os.environ.get("PYFUSE_SANDBOX_DOCKER_CONTAINER", "pyfuse-sandbox")
    image = os.environ.get("PYFUSE_SANDBOX_DOCKER_IMAGE", "pyfuse-sandbox")

    if await _container_exists(container):
        if await _container_running(container):
            print(f"Stopping container '{container}'...")
            await _docker_wait("stop", container)
        print(f"Removing container '{container}'...")
        await _docker_wait("rm", container)

    if await _image_exists(image):
        print(f"Removing image '{image}'...")
        await _docker_wait("rmi", image)

    print("Done.")


def _add_sandbox_parser(sub: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    p = sub.add_parser("sandbox", help="Manage the Docker sandbox")
    sandbox_sub = p.add_subparsers(dest="sandbox_action")
    sandbox_sub.add_parser("setup", help="Build the Docker sandbox image")
    sandbox_sub.add_parser("status", help="Show Docker sandbox status")
    sandbox_sub.add_parser("teardown", help="Stop and remove the Docker sandbox")


# ---------------------------------------------------------------------------
# keypair command
# ---------------------------------------------------------------------------


def _cmd_keypair(args: argparse.Namespace) -> None:
    """Handle ``pyfuse keypair generate|fingerprint`` subcommands."""
    action = getattr(args, "keypair_action", None)
    if action is None:
        print("Usage: pyfuse keypair {generate|fingerprint}", file=sys.stderr)
        sys.exit(1)

    from pyfuse.core.signing import KeyPair

    if action == "generate":
        out = Path(args.output) if args.output else Path("pyfuse_key.pem")
        pub_out = out.with_suffix(".pub")
        kp = KeyPair.generate()
        kp.save(out)
        kp.save_public(pub_out)
        print(f"Private key: {out}")
        print(f"Public key:  {pub_out}")
        print(f"Fingerprint: {kp.fingerprint}")
        print()
        print(f"Share {pub_out.name} with workers (copy to their --trusted-keys directory).")

    elif action == "fingerprint":
        path = Path(args.key_file)
        if not path.exists():
            print(f"Error: file not found: {path}", file=sys.stderr)
            sys.exit(1)
        try:
            kp = KeyPair.from_file(path)
            print(kp.fingerprint)
        except Exception:
            from pyfuse.core.signing import _load_public_key_bytes, _fingerprint

            raw = _load_public_key_bytes(path)
            print(_fingerprint(raw))


def _add_keypair_parser(sub: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    p = sub.add_parser("keypair", help="Manage Ed25519 signing key pairs")
    kp_sub = p.add_subparsers(dest="keypair_action")

    gen = kp_sub.add_parser("generate", help="Generate a new Ed25519 key pair")
    gen.add_argument(
        "-o", "--output", default=None,
        help="Output path for private key (default: pyfuse_key.pem)",
    )

    fp = kp_sub.add_parser("fingerprint", help="Print the fingerprint of a key file")
    fp.add_argument("key_file", help="Path to a .pem or .pub key file")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pyfuse", description="pyfuse - distributed task execution",
    )
    sub = parser.add_subparsers(dest="command")
    _add_worker_parser(sub)
    _add_run_parser(sub)
    sub.add_parser("info", help="Show pyfuse configuration")
    _add_serialize_parser(sub)
    _add_reconstruct_parser(sub)
    _add_sandbox_parser(sub)
    _add_keypair_parser(sub)
    return parser


_COMMAND_HANDLERS: dict[str, Callable[[argparse.Namespace], None]] = {
    "worker": _cmd_worker,
    "run": _cmd_run,
    "info": _cmd_info,
    "serialize": _cmd_serialize,
    "reconstruct": _cmd_reconstruct,
    "sandbox": _cmd_sandbox,
    "keypair": _cmd_keypair,
}


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        sys.exit(1)
    _COMMAND_HANDLERS[args.command](args)


if __name__ == "__main__":
    main()
