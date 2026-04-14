"""Docker-based sandbox executor.

Runs the :mod:`~pyfuse.worker.sandbox.guest_agent` inside a Docker
container, communicating over the same TCP + length-prefixed JSON
protocol used by the VM executor.

Requirements
~~~~~~~~~~~~
* Docker Engine installed and the ``docker`` CLI available on ``PATH``
* The current user must be able to run ``docker`` commands (i.e. be in
  the ``docker`` group or use rootless Docker)

The image is built automatically from the bundled ``Dockerfile`` on
first use, so ``pyfuse sandbox setup --docker`` is optional (but
recommended in CI to avoid a cold-start build).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from pyfuse.core.errors import WorkerError
from pyfuse.worker.sandbox._protocol import async_recv, async_send
from pyfuse.worker.sandbox.base import SandboxExecutor
from pyfuse.worker.sandbox.config import SandboxConfig

logger = logging.getLogger(__name__)

_DOCKERFILE_DIR = Path(__file__).resolve().parent  # contains Dockerfile + guest_agent.py
_DEFAULT_IMAGE = "pyfuse-sandbox"
_DEFAULT_CONTAINER = "pyfuse-sandbox"


class DockerExecutor(SandboxExecutor):
    """Execute functions inside a Docker container.

    The executor lazily starts a container on first use and keeps it
    running for the lifetime of the worker so that subsequent task
    executions reuse the same guest agent connection.

    Parameters
    ----------
    config
        Sandbox configuration (image name, ports, resources …).
    """

    def __init__(self, config: SandboxConfig | None = None) -> None:
        self._cfg = config or SandboxConfig(enabled=True, backend="docker")
        self._host_port: int | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._started = False

    # -- SandboxExecutor interface -------------------------------------------

    async def execute(
        self,
        source: str,
        function_name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        owner_class: str | None = None,
    ) -> Any:
        if not self._started:
            await self.start()

        request: dict[str, Any] = {
            "source": source,
            "function_name": function_name,
            "args": list(args),
            "kwargs": kwargs,
        }
        if owner_class is not None:
            request["owner_class"] = owner_class

        try:
            response = await asyncio.wait_for(
                self._send_request(request),
                timeout=self._cfg.timeout,
            )
        except asyncio.TimeoutError:
            raise WorkerError(
                f"Docker sandbox execution of '{function_name}' timed out "
                f"after {self._cfg.timeout}s"
            ) from None

        if response["status"] == "error":
            raise WorkerError(
                f"Sandbox error — {response.get('error_type', 'Unknown')}: "
                f"{response.get('error_message', '')}"
            )
        return response.get("result")

    async def start(self) -> None:
        """Build the image (if needed), start the container, connect."""
        _check_docker_available()

        # Build the image if it doesn't exist.
        if not await _image_exists(self._cfg.docker_image):
            logger.info("Building Docker image '%s' …", self._cfg.docker_image)
            await _build_image(self._cfg.docker_image)

        # Start the container if it's not running.
        container = self._cfg.docker_container_name
        if not await _container_running(container):
            # Remove a stopped container with the same name, if any.
            if await _container_exists(container):
                await _docker_wait("rm", "-f", container)

            logger.info("Starting container '%s' …", container)
            await self._run_container()

        # Determine the host port mapped to the guest agent.
        self._host_port = await _mapped_port(container, self._cfg.guest_port)

        # Wait for the agent to be reachable.
        await self._wait_for_agent()

        # Connect.
        await self._connect()
        self._started = True
        logger.info(
            "Docker sandbox ready (localhost:%d → container:%d)",
            self._host_port,
            self._cfg.guest_port,
        )

    async def stop(self) -> None:
        """Disconnect and stop the container."""
        if self._writer is not None:
            self._writer.close()
            self._reader = self._writer = None
        container = self._cfg.docker_container_name
        if await _container_running(container):
            logger.info("Stopping container '%s' …", container)
            await _docker_wait("stop", container)
        self._started = False

    # -- internals -----------------------------------------------------------

    async def _run_container(self) -> None:
        """Start a new container from the sandbox image."""
        cmd = [
            "docker", "run", "-d",
            "--name", self._cfg.docker_container_name,
            "-p", f"0:{self._cfg.guest_port}",  # random host port
        ]
        if self._cfg.cpus:
            cmd += ["--cpus", str(self._cfg.cpus)]
        if self._cfg.memory_gb:
            cmd += ["--memory", f"{self._cfg.memory_gb}g"]
        cmd.append(self._cfg.docker_image)

        rc, stdout, stderr = await _docker_wait(*cmd[1:])  # _docker_wait prepends "docker"
        if rc != 0:
            raise WorkerError(
                f"Failed to start Docker container: {stderr.strip()}"
            )

    async def _wait_for_agent(self) -> None:
        """Poll the guest agent port until it is reachable."""
        assert self._host_port is not None
        for _ in range(int(self._cfg.boot_timeout)):
            try:
                _r, _w = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", self._host_port),
                    timeout=2.0,
                )
                _w.close()
                return
            except (OSError, asyncio.TimeoutError):
                await asyncio.sleep(1)
        raise WorkerError(
            f"Docker guest agent did not become reachable within "
            f"{self._cfg.boot_timeout}s"
        )

    async def _connect(self) -> None:
        assert self._host_port is not None
        self._reader, self._writer = await asyncio.open_connection(
            "127.0.0.1", self._host_port,
        )

    async def _send_request(self, request: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            if self._reader is None or self._writer is None:
                await self._connect()
            assert self._reader is not None and self._writer is not None
            try:
                await async_send(self._writer, request)
                return await async_recv(self._reader)
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                # Reconnect on failure.
                self._reader = self._writer = None
                await self._connect()
                assert self._reader is not None and self._writer is not None
                await async_send(self._writer, request)
                return await async_recv(self._reader)


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
    """Check whether a Docker image exists locally."""
    rc, _, _ = await _docker_wait("image", "inspect", image)
    return rc == 0


async def _build_image(image: str) -> None:
    """Build the sandbox Docker image from the bundled Dockerfile."""
    rc, stdout, stderr = await _docker_wait(
        "build", "-t", image, str(_DOCKERFILE_DIR),
    )
    if rc != 0:
        raise WorkerError(
            f"Failed to build Docker image '{image}':\n{stderr.strip()}"
        )
    logger.info("Docker image '%s' built successfully.", image)


async def _container_exists(name: str) -> bool:
    """Check whether a Docker container (running or stopped) exists."""
    rc, _, _ = await _docker_wait("container", "inspect", name)
    return rc == 0


async def _container_running(name: str) -> bool:
    """Check whether a Docker container is currently running."""
    rc, stdout, _ = await _docker_wait(
        "inspect", "-f", "{{.State.Running}}", name,
    )
    return rc == 0 and stdout.strip().lower() == "true"


async def _mapped_port(container: str, guest_port: int) -> int:
    """Return the host port mapped to *guest_port* in *container*."""
    rc, stdout, stderr = await _docker_wait(
        "port", container, str(guest_port),
    )
    if rc != 0:
        raise WorkerError(
            f"Could not determine mapped port for {container}:{guest_port}: "
            f"{stderr.strip()}"
        )
    # Output looks like "0.0.0.0:12345\n" or ":::12345\n"
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


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_docker_executor(config: SandboxConfig | None = None) -> DockerExecutor:
    """Create a :class:`DockerExecutor` with sensible defaults.

    Reads ``PYFUSE_SANDBOX_DOCKER_IMAGE`` from the environment when
    *config* is not provided.
    """
    if config is not None:
        return DockerExecutor(config)

    image = os.environ.get("PYFUSE_SANDBOX_DOCKER_IMAGE", _DEFAULT_IMAGE)
    container = os.environ.get("PYFUSE_SANDBOX_DOCKER_CONTAINER", _DEFAULT_CONTAINER)
    return DockerExecutor(SandboxConfig(
        enabled=True,
        backend="docker",
        docker_image=image,
        docker_container_name=container,
    ))
