"""Configuration for the sandbox subsystem."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SandboxConfig:
    """Settings that control how (and whether) function execution is sandboxed.

    Parameters
    ----------
    enabled
        ``True`` to run user code inside an isolated sandbox.
        When ``False`` the :class:`NoopExecutor` is used (current
        behaviour).
    backend
        Which isolation technology to use.  ``"vm"`` uses *tart*
        micro-VMs (Apple Silicon only), ``"docker"`` uses Docker
        containers.  Defaults to ``"vm"``.
    vm_name
        Name of the ``tart`` virtual machine.  The default is
        ``pyfuse-sandbox``.
    guest_port
        TCP port the guest agent listens on *inside* the VM / container.
    cpus
        Number of vCPUs allocated to the VM / container.
    memory_gb
        Gigabytes of RAM allocated to the VM / container.
    timeout
        Maximum seconds to wait for a single function execution inside
        the sandbox before killing it.
    boot_timeout
        Maximum seconds to wait for the sandbox to become reachable
        after starting it.
    ssh_key_path
        Path to the SSH private key used to connect to the VM guest.
        When *None*, the setup script's default location
        (``~/.pyfuse/sandbox/id_ed25519``) is used.  Only used by the
        ``"vm"`` backend.
    docker_image
        Docker image to use for the sandbox container.  The default is
        ``pyfuse-sandbox``.  If the configured image does not already
        exist locally, the Docker backend may build it from the bundled
        Dockerfile on first use.
    docker_container_name
        Name assigned to the running container (for easy management).
    """

    enabled: bool = False
    backend: str = "vm"
    vm_name: str = "pyfuse-sandbox"
    guest_port: int = 9749
    cpus: int = 2
    memory_gb: int = 2
    timeout: float = 60.0
    boot_timeout: float = 30.0
    ssh_key_path: str | None = None
    extra_pip_packages: list[str] = field(default_factory=list)
    docker_image: str = "pyfuse-sandbox"
    docker_container_name: str = "pyfuse-sandbox"
