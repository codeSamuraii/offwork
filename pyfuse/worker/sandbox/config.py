"""Configuration for the sandbox subsystem."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SandboxConfig:
    """Settings that control how (and whether) function execution is sandboxed.

    Parameters
    ----------
    enabled
        ``True`` to run user code inside an isolated micro-VM.
        When ``False`` the :class:`NoopExecutor` is used (current
        behaviour).
    vm_name
        Name of the ``tart`` virtual machine.  The default is
        ``pyfuse-sandbox``.
    guest_port
        TCP port the guest agent listens on *inside* the VM.
    cpus
        Number of vCPUs allocated to the VM.
    memory_gb
        Gigabytes of RAM allocated to the VM.
    timeout
        Maximum seconds to wait for a single function execution inside
        the VM before killing it.
    boot_timeout
        Maximum seconds to wait for the VM to become reachable after
        starting it.
    ssh_key_path
        Path to the SSH private key used to connect to the VM guest.
        When *None*, the setup script's default location
        (``~/.pyfuse/sandbox/id_ed25519``) is used.
    """

    enabled: bool = False
    vm_name: str = "pyfuse-sandbox"
    guest_port: int = 9749
    cpus: int = 2
    memory_gb: int = 2
    timeout: float = 60.0
    boot_timeout: float = 30.0
    ssh_key_path: str | None = None
    extra_pip_packages: list[str] = field(default_factory=list)
