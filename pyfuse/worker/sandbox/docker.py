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
import logging
from typing import Any
from pathlib import Path
from collections.abc import Callable

from pyfuse.core.errors import WorkerError
from pyfuse.core.progress import _progress_callback
from pyfuse.worker.sandbox._protocol import async_recv, async_send

logger = logging.getLogger(__name__)

_DOCKERFILE_DIR = Path(__file__).resolve().parent  # contains Dockerfile + guest_agent.py
_DEFAULT_IMAGE = "pyfuse-sandbox"
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
            "args": list(args),
            "kwargs": kwargs,
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

        if response["status"] == "error":
            raise WorkerError(
                f"Sandbox error — {response.get('error_type', 'Unknown')}: "
                f"{response.get('error_message', '')}"
            )
        return response.get("result")

    async def start(self) -> None:
        """Build the image (if needed), start the container, connect."""
        _check_docker_available()

        if not await _image_exists(self.image):
            logger.info("Building Docker image '%s' …", self.image)
            await _build_image(self.image)

        container = self.container_name
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
        assert self._host_port is not None
        for _ in range(int(self.boot_timeout)):
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
            f"Docker guest agent did not become reachable within {self.boot_timeout}s"
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
            try:
                await async_send(self._writer, request)
                return await self._read_response(progress_cb)
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                self._reader = self._writer = None
                await self._connect()
                assert self._reader is not None and self._writer is not None
                await async_send(self._writer, request)
                return await self._read_response(progress_cb)

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
