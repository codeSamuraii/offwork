"""VM-based sandbox executor using *tart* on Apple Silicon.

`tart <https://github.com/cirruslabs/tart>`_ is a lightweight
virtualisation tool built on Apple's ``Virtualization.framework``.
This executor boots a Linux micro-VM, starts a
:mod:`~pyfuse.worker.sandbox.guest_agent` inside it, and forwards
every execution request over TCP.

Requirements
~~~~~~~~~~~~
* macOS on Apple Silicon (``arm64``)
* ``tart`` installed (``brew install cirruslabs/cli/tart``)
* A VM image prepared via ``pyfuse sandbox setup`` (or the provided
  ``scripts/setup_sandbox_macos.sh`` script)
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

_PYFUSE_DIR = Path.home() / ".pyfuse" / "sandbox"
_DEFAULT_SSH_KEY = _PYFUSE_DIR / "id_ed25519"


class VMExecutor(SandboxExecutor):
    """Execute functions inside a *tart* micro-VM.

    The executor lazily boots the VM on first use and keeps it running
    for the lifetime of the worker so that subsequent task executions
    reuse the same guest agent connection.

    Parameters
    ----------
    config
        Sandbox configuration (VM name, ports, resources …).
    """

    def __init__(self, config: SandboxConfig | None = None) -> None:
        self._cfg = config or SandboxConfig(enabled=True)
        self._vm_ip: str | None = None
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

        request = {
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
                f"Sandbox execution of '{function_name}' timed out "
                f"after {self._cfg.timeout}s"
            ) from None

        if response["status"] == "error":
            raise WorkerError(
                f"Sandbox error — {response.get('error_type', 'Unknown')}: "
                f"{response.get('error_message', '')}"
            )
        return response.get("result")

    async def start(self) -> None:
        """Boot the VM and establish a connection to the guest agent."""
        _check_tart_available()

        if not await _vm_exists(self._cfg.vm_name):
            raise WorkerError(
                f"VM '{self._cfg.vm_name}' not found. "
                f"Run 'pyfuse sandbox setup' first."
            )

        # Start the VM if it's not already running.
        if not await _vm_running(self._cfg.vm_name):
            logger.info("Starting VM '%s' …", self._cfg.vm_name)
            await _tart("run", self._cfg.vm_name, "--no-graphics")
            await self._wait_for_vm_ip()

        if self._vm_ip is None:
            await self._wait_for_vm_ip()

        # Ensure the guest agent is running.
        await self._ensure_guest_agent()

        # Connect to the guest agent.
        await self._connect()
        self._started = True
        logger.info(
            "VM sandbox ready (%s:%d)", self._vm_ip, self._cfg.guest_port,
        )

    async def stop(self) -> None:
        """Disconnect and (optionally) stop the VM."""
        if self._writer is not None:
            self._writer.close()
            self._reader = self._writer = None
        if await _vm_running(self._cfg.vm_name):
            logger.info("Stopping VM '%s' …", self._cfg.vm_name)
            await _tart_wait("stop", self._cfg.vm_name)
        self._started = False

    # -- internals -----------------------------------------------------------

    async def _wait_for_vm_ip(self) -> None:
        """Poll ``tart ip`` until the VM is reachable."""
        for _ in range(int(self._cfg.boot_timeout)):
            ip = await _tart_output("ip", self._cfg.vm_name)
            ip = ip.strip()
            if ip:
                self._vm_ip = ip
                logger.debug("VM IP: %s", ip)
                return
            await asyncio.sleep(1)
        raise WorkerError(
            f"VM '{self._cfg.vm_name}' did not obtain an IP within "
            f"{self._cfg.boot_timeout}s"
        )

    async def _ensure_guest_agent(self) -> None:
        """Start the guest agent inside the VM via SSH if not reachable."""
        assert self._vm_ip is not None
        # Quick connectivity check
        try:
            _r, _w = await asyncio.wait_for(
                asyncio.open_connection(self._vm_ip, self._cfg.guest_port),
                timeout=2.0,
            )
            _w.close()
            return  # agent already running
        except (OSError, asyncio.TimeoutError):
            pass

        ssh_key = self._cfg.ssh_key_path or str(_DEFAULT_SSH_KEY)
        agent_script = str(
            Path(__file__).with_name("guest_agent.py").resolve()
        )
        # Deploy and start the agent over SSH.
        ssh_base = [
            "ssh",
            "-i", ssh_key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            f"pyfuse@{self._vm_ip}",
        ]

        # Copy the agent script.
        scp_cmd = [
            "scp",
            "-i", ssh_key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            agent_script,
            f"pyfuse@{self._vm_ip}:/tmp/guest_agent.py",
        ]
        proc = await asyncio.create_subprocess_exec(
            *scp_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        # Start agent in background via a shell so redirections work.
        agent_cmd = (
            f"nohup python3 /tmp/guest_agent.py "
            f"--port {self._cfg.guest_port} "
            f"</dev/null >/tmp/guest_agent.log 2>&1 &"
        )
        start_cmd = ssh_base + [agent_cmd]
        proc = await asyncio.create_subprocess_exec(
            *start_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        # Wait for agent to be reachable.
        for _ in range(10):
            try:
                _r, _w = await asyncio.wait_for(
                    asyncio.open_connection(self._vm_ip, self._cfg.guest_port),
                    timeout=2.0,
                )
                _w.close()
                logger.info("Guest agent started successfully.")
                return
            except (OSError, asyncio.TimeoutError):
                await asyncio.sleep(1)
        raise WorkerError("Guest agent failed to start inside the VM")

    async def _connect(self) -> None:
        assert self._vm_ip is not None
        self._reader, self._writer = await asyncio.open_connection(
            self._vm_ip, self._cfg.guest_port,
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
# tart CLI helpers
# ---------------------------------------------------------------------------


def _check_tart_available() -> None:
    if shutil.which("tart") is None:
        raise WorkerError(
            "'tart' command not found. Install it with: "
            "brew install cirruslabs/cli/tart"
        )


async def _tart(*args: str) -> asyncio.subprocess.Process:
    """Launch ``tart <args>`` without waiting for completion."""
    return await asyncio.create_subprocess_exec(
        "tart", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _tart_wait(*args: str) -> tuple[int, str, str]:
    """Run ``tart <args>`` and wait for it to finish."""
    proc = await asyncio.create_subprocess_exec(
        "tart", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout_bytes.decode() if stdout_bytes else "",
        stderr_bytes.decode() if stderr_bytes else "",
    )


async def _tart_output(*args: str) -> str:
    """Run ``tart <args>`` and return stdout."""
    _, stdout, _ = await _tart_wait(*args)
    return stdout


async def _vm_exists(name: str) -> bool:
    """Check whether a tart VM with the given name exists."""
    rc, stdout, _ = await _tart_wait("list", "--format", "json")
    if rc != 0:
        return False
    try:
        vms = json.loads(stdout)
        return any(vm.get("Name") == name or vm.get("name") == name for vm in vms)
    except (json.JSONDecodeError, TypeError):
        return False


async def _vm_running(name: str) -> bool:
    """Check whether a tart VM is currently running."""
    rc, stdout, _ = await _tart_wait("list", "--format", "json")
    if rc != 0:
        return False
    try:
        vms = json.loads(stdout)
        for vm in vms:
            vm_name = vm.get("Name") or vm.get("name")
            status = vm.get("State") or vm.get("state") or vm.get("status", "")
            if vm_name == name and status.lower() in ("running", "started"):
                return True
        return False
    except (json.JSONDecodeError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_vm_executor(config: SandboxConfig | None = None) -> VMExecutor:
    """Create a :class:`VMExecutor` with sensible defaults.

    Reads ``PYFUSE_SANDBOX_VM`` and ``PYFUSE_SANDBOX_SSH_KEY`` from
    the environment when *config* is not provided.
    """
    if config is not None:
        return VMExecutor(config)

    vm_name = os.environ.get("PYFUSE_SANDBOX_VM", "pyfuse-sandbox")
    ssh_key = os.environ.get("PYFUSE_SANDBOX_SSH_KEY")
    return VMExecutor(SandboxConfig(
        enabled=True,
        vm_name=vm_name,
        ssh_key_path=ssh_key,
    ))
