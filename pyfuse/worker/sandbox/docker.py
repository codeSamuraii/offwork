"""Docker-based sandbox for isolated function execution.

Runs the guest agent (:mod:`~pyfuse.worker.sandbox.guest_agent`) inside
a Docker container, communicating over TCP with a length-prefixed JSON
protocol.

Requirements
~~~~~~~~~~~~
* Docker (or a compatible runtime such as colima / Podman) installed
  and the ``docker`` CLI available on ``PATH``
* The current user must be able to run ``docker`` commands (i.e. be in
  the ``docker`` group or use rootless Docker)

The image is built automatically from the bundled ``Dockerfile`` on
first use, so ``pyfuse sandbox setup`` is optional (but recommended in
CI to avoid a cold-start build).
"""

import os
import shutil
import asyncio
import hashlib
import logging
import contextlib
from typing import Any
from pathlib import Path
from collections.abc import Callable

from pyfuse.core.errors import WorkerError
from pyfuse.core.task import _resolve, _to_jsonable
from pyfuse.core.progress import _progress_callback
from pyfuse.worker.sandbox._protocol import async_recv, async_send

logger = logging.getLogger(__name__)

_DOCKERFILE_DIR = Path(__file__).resolve().parent  # contains Dockerfile + guest_agent.py
_IMAGE_ASSETS = ("Dockerfile", "guest_agent.py", "_protocol.py")


def _assets_digest() -> str:
    """Short hash of the files baked into the sandbox image.

    Used as the default image tag so a code change in the guest agent
    or Dockerfile produces a fresh image instead of silently reusing a
    stale one whose ``guest_agent.py`` no longer matches the host.
    """
    h = hashlib.sha256()
    for name in _IMAGE_ASSETS:
        path = _DOCKERFILE_DIR / name
        if path.exists():
            h.update(name.encode())
            h.update(b"\0")
            h.update(path.read_bytes())
            h.update(b"\0")
    return h.hexdigest()[:12]


_DEFAULT_IMAGE = f"pyfuse-sandbox:{_assets_digest()}"
_DEFAULT_CONTAINER = "pyfuse-sandbox"


class DockerSandbox:
    """Execute functions inside a Docker container.

    The sandbox lazily starts a container on first use and keeps it
    running for the lifetime of the worker so that subsequent task
    executions reuse the same guest agent connection.

    Parameters
    ----------
    image
        Docker image name.  Built automatically from the bundled
        ``Dockerfile`` if it doesn't exist locally.
    container_name
        Name assigned to the running container.
    guest_port
        TCP port the guest agent listens on inside the container.
    cpus
        Number of vCPUs allocated to the container.
    memory_gb
        Gigabytes of RAM allocated to the container.
    timeout
        Maximum seconds for a single function execution.
    boot_timeout
        Maximum seconds to wait for the container to become reachable.
    """

    def __init__(
        self,
        *,
        image: str = _DEFAULT_IMAGE,
        container_name: str = _DEFAULT_CONTAINER,
        guest_port: int = 9749,
        cpus: int = 2,
        memory_gb: int = 2,
        timeout: float = 60.0,
        boot_timeout: float = 30.0,
    ) -> None:
        if cpus < 1:
            raise ValueError(f"cpus must be at least 1, got {cpus}")
        if memory_gb < 1:
            raise ValueError(f"memory_gb must be at least 1, got {memory_gb}")
        if timeout <= 0:
            raise ValueError(f"timeout must be positive, got {timeout}")
        if boot_timeout <= 0:
            raise ValueError(f"boot_timeout must be positive, got {boot_timeout}")

        self.image = image
        self.container_name = container_name
        self.guest_port = guest_port
        self.cpus = cpus
        self.memory_gb = memory_gb
        self.timeout = timeout
        self.boot_timeout = boot_timeout

        self._host_port: int | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._started = False

    # -- public API ----------------------------------------------------------

    async def execute(
        self,
        source: str,
        function_name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        owner_class: str | None = None,
    ) -> Any:
        """Send *source* + *function_name* to the guest agent and return the result."""
        if not self._started:
            await self.start()

        request: dict[str, Any] = {
            "source": source,
            "function_name": function_name,
            "args": [_to_jsonable(a) for a in args],
            "kwargs": {k: _to_jsonable(v) for k, v in kwargs.items()},
        }
        if owner_class is not None:
            request["owner_class"] = owner_class

        # Pick up the host-side progress callback (set by _handle_task)
        # so we can forward progress messages from the container.
        progress_cb = _progress_callback.get(None)

        try:
            response = await asyncio.wait_for(
                self._send_request(request, progress_cb=progress_cb),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            raise WorkerError(
                f"Sandbox execution of '{function_name}' timed out "
                f"after {self.timeout}s"
            ) from None
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
            raise WorkerError(
                f"Sandbox connection lost while executing '{function_name}': "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        if response["status"] == "error":
            raise WorkerError(
                f"Sandbox error — {response.get('error_type', 'Unknown')}: "
                f"{response.get('error_message', '')}"
            )
        # Return values from the guest agent travel through the same
        # sentinel encoding so non-JSON-native types (tuples, datetimes,
        # custom classes, etc.) round-trip transparently.
        return _resolve(response.get("result"), {})

    async def start(self) -> None:
        """Build the image (if needed), start the container, connect."""
        _check_docker_available()

        if not await _image_exists(self.image):
            logger.info("Building Docker image '%s' …", self.image)
            await _build_image(self.image)

        container = self.container_name
        if await _container_exists(container):
            current_image = await _container_image(container)
            if current_image != self.image:
                logger.info(
                    "Container '%s' was built from a different image (%s); "
                    "recreating from '%s'",
                    container, current_image or "<unknown>", self.image,
                )
                await _docker_wait("rm", "-f", container)

        if not await _container_running(container):
            if await _container_exists(container):
                await _docker_wait("rm", "-f", container)
            logger.info("Starting container '%s' …", container)
            await self._run_container()

        self._host_port = await _mapped_port(container, self.guest_port)
        await self._wait_for_agent()
        await self._connect()
        self._started = True
        logger.info(
            "Docker sandbox ready (localhost:%d → container:%d)",
            self._host_port,
            self.guest_port,
        )

    async def stop(self) -> None:
        """Disconnect and stop the container."""
        if self._writer is not None:
            self._writer.close()
            self._reader = self._writer = None
        container = self.container_name
        if await _container_running(container):
            logger.info("Stopping container '%s' …", container)
            await _docker_wait("stop", container)
        self._started = False

    async def __aenter__(self) -> "DockerSandbox":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # -- internals -----------------------------------------------------------

    async def _run_container(self) -> None:
        cmd = [
            "docker", "run", "-d",
            "--name", self.container_name,
            "-p", f"0:{self.guest_port}",
        ]
        if self.cpus:
            cmd += ["--cpus", str(self.cpus)]
        if self.memory_gb:
            cmd += ["--memory", f"{self.memory_gb}g"]
        cmd.append(self.image)

        rc, _stdout, stderr = await _docker_wait(*cmd[1:])
        if rc != 0:
            raise WorkerError(f"Failed to start Docker container: {stderr.strip()}")

    async def _wait_for_agent(self) -> None:
        """Block until the guest agent is actually answering on the wire.

        A bare TCP ``open_connection`` is not sufficient on Linux: when
        a container port is published, ``docker-proxy`` accepts host
        connections *before* the in-container process is listening,
        which causes the very first request to race with agent startup
        and come back as ``IncompleteReadError``.  Performing an actual
        ping/pong exchange guarantees end-to-end readiness.
        """
        assert self._host_port is not None
        deadline = asyncio.get_running_loop().time() + self.boot_timeout
        last_err: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", self._host_port),
                    timeout=2.0,
                )
                try:
                    await asyncio.wait_for(
                        async_send(writer, {"op": "ping"}), timeout=2.0,
                    )
                    msg = await asyncio.wait_for(async_recv(reader), timeout=2.0)
                finally:
                    writer.close()
                    with contextlib.suppress(ConnectionError, OSError):
                        await writer.wait_closed()
                if msg.get("status") == "pong":
                    return
                last_err = WorkerError(f"Unexpected handshake reply: {msg!r}")
            except (
                OSError,
                asyncio.TimeoutError,
                asyncio.IncompleteReadError,
                ConnectionError,
            ) as exc:
                last_err = exc
            await asyncio.sleep(0.5)
        raise WorkerError(
            f"Docker guest agent did not become reachable within "
            f"{self.boot_timeout}s: {last_err!r}"
        )

    async def _connect(self) -> None:
        assert self._host_port is not None
        self._reader, self._writer = await asyncio.open_connection(
            "127.0.0.1", self._host_port,
        )

    async def _send_request(
        self,
        request: dict[str, Any],
        *,
        progress_cb: Callable[..., Any] | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            if self._reader is None or self._writer is None:
                await self._connect()
            assert self._reader is not None and self._writer is not None
            success = False
            try:
                try:
                    await async_send(self._writer, request)
                    result = await self._read_response(progress_cb)
                    success = True
                    return result
                except (asyncio.IncompleteReadError, ConnectionError, OSError):
                    self._reader = self._writer = None
                    await self._connect()
                    assert self._reader is not None and self._writer is not None
                    await async_send(self._writer, request)
                    result = await self._read_response(progress_cb)
                    success = True
                    return result
            finally:
                if not success:
                    # On cancellation or timeout the guest agent may still
                    # be processing and will eventually write a stale
                    # response.  Reset the connection so the next request
                    # doesn't read that leftover data.
                    if self._writer is not None:
                        self._writer.close()
                    self._reader = self._writer = None

    async def _read_response(
        self,
        progress_cb: Callable[..., Any] | None = None,
    ) -> dict[str, Any]:
        """Read messages until a terminal (ok/error) response arrives.

        Intermediate ``{"status": "progress", ...}`` frames are forwarded
        to *progress_cb* so that ``pyfuse.progress()`` calls inside the
        container surface on the host in real time.
        """
        assert self._reader is not None
        while True:
            msg = await async_recv(self._reader)
            if msg.get("status") == "progress":
                if progress_cb is not None:
                    progress_cb(
                        msg.get("current", 0),
                        msg.get("total"),
                        msg.get("message"),
                    )
                continue
            return msg


# ---------------------------------------------------------------------------
# Docker CLI helpers
# ---------------------------------------------------------------------------


def _check_docker_available() -> None:
    if shutil.which("docker") is None:
        raise WorkerError(
            "'docker' command not found. "
            "Install Docker from https://docs.docker.com/get-docker/"
        )


async def _docker_wait(*args: str) -> tuple[int, str, str]:
    """Run ``docker <args>`` and wait for it to finish."""
    proc = await asyncio.create_subprocess_exec(
        "docker", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout_bytes.decode() if stdout_bytes else "",
        stderr_bytes.decode() if stderr_bytes else "",
    )


async def _image_exists(image: str) -> bool:
    rc, _, _ = await _docker_wait("image", "inspect", image)
    return rc == 0


async def _build_image(image: str) -> None:
    rc, _stdout, stderr = await _docker_wait(
        "build", "-t", image, str(_DOCKERFILE_DIR),
    )
    if rc != 0:
        raise WorkerError(f"Failed to build Docker image '{image}':\n{stderr.strip()}")
    logger.info("Docker image '%s' built successfully.", image)


async def _container_exists(name: str) -> bool:
    rc, _, _ = await _docker_wait("container", "inspect", name)
    return rc == 0


async def _container_running(name: str) -> bool:
    rc, stdout, _ = await _docker_wait(
        "inspect", "-f", "{{.State.Running}}", name,
    )
    return rc == 0 and stdout.strip().lower() == "true"


async def _container_image(name: str) -> str | None:
    """Return the image (with tag) the container was created from, or None."""
    rc, stdout, _ = await _docker_wait(
        "inspect", "-f", "{{.Config.Image}}", name,
    )
    if rc != 0:
        return None
    image = stdout.strip()
    return image or None


async def _mapped_port(container: str, guest_port: int) -> int:
    rc, stdout, stderr = await _docker_wait("port", container, str(guest_port))
    if rc != 0:
        raise WorkerError(
            f"Could not determine mapped port for {container}:{guest_port}: "
            f"{stderr.strip()}"
        )
    for line in stdout.strip().splitlines():
        parts = line.rsplit(":", 1)
        if len(parts) == 2:
            try:
                return int(parts[1])
            except ValueError:
                continue
    raise WorkerError(
        f"Unexpected 'docker port' output for {container}:{guest_port}: "
        f"{stdout.strip()!r}"
    )
