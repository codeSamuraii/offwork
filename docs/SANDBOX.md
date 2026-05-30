# Sandbox

Run worker tasks inside isolated Docker containers. Sandboxing is transparent to clients -- no code changes required on the sender side.

## Overview

By default, workers execute tasks in the host process. With `--sandbox`, execution is delegated to a guest agent running inside a Docker container. The worker still handles caching, dependency resolution, and retries on the host; only the `compile()` → `exec()` → call step moves into the sandbox.

## Requirements

- Docker installed and running (`docker info` should succeed)
- The current user can run `docker` commands (docker group or rootless Docker)

## Setup

```bash
offwork sandbox setup
```

This builds the `offwork-sandbox` Docker image from the bundled Dockerfile. The image is based on `python:3.12-slim` and contains only the stdlib-only guest agent -- no offwork installation needed inside the container.

You can also build manually:

```bash
bash helpers/setup_sandbox_docker.sh
```

Or let the worker build automatically on first use (the image is built lazily if it doesn't exist).

## Start a sandboxed worker

```bash
offwork worker --backend redis://localhost:6379 --sandbox
```

The worker starts a container on first task, keeps it running for the worker's lifetime, and stops it on shutdown.

## Customization

Override the image and container names via environment variables:

```bash
export OFFWORK_SANDBOX_DOCKER_IMAGE=my-custom-image
export OFFWORK_SANDBOX_DOCKER_CONTAINER=my-sandbox
offwork worker --backend redis://localhost:6379 --sandbox
```

## Management commands

```bash
# Check sandbox status
offwork sandbox status

# Remove the Docker container and image
offwork sandbox teardown
```

Example `offwork sandbox status` output:

```
Docker:
  docker: installed
  Image 'offwork-sandbox': exists
  Container 'offwork-sandbox': not found
```

## Programmatic usage

```python
from offwork.worker.sandbox import DockerSandbox

async with DockerSandbox() as sandbox:
    result = await sandbox.execute(source, "my_func", (arg1,), {})
```

Pass `sandbox=True` to `serve()`:

```python
await offwork.serve("redis://localhost:6379", sandbox=True)
```

Or provide a `DockerSandbox` instance for custom settings:

```python
from offwork.worker.sandbox import DockerSandbox

sandbox = DockerSandbox(cpus=4, memory_gb=4)
await offwork.serve("redis://localhost:6379", sandbox=sandbox)
```

## Configuration reference

`DockerSandbox` accepts the following keyword arguments:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `image` | `"offwork-sandbox"` | Docker image name |
| `container_name` | `"offwork-sandbox"` | Container name |
| `guest_port` | `9749` | TCP port the guest agent listens on |
| `cpus` | `2` | vCPUs allocated to the container |
| `memory_gb` | `2` | RAM (GB) allocated to the container |
| `timeout` | `60.0` | Max seconds per function execution |
| `boot_timeout` | `30.0` | Max seconds to wait for container to start |

## How it works

The sandbox uses a guest agent (`guest_agent.py`) — a lightweight, stdlib-only Python script deployed inside the container. The worker communicates with it over TCP using a length-prefixed JSON protocol:

1. Worker sends the reconstructed source code, function name, and arguments
2. Guest agent `exec`s the source, calls the function, and returns the result
3. Errors are serialized with type, message, and traceback

The sandbox stays running between tasks. The first execution incurs a startup cost (container boot + agent connection), but subsequent calls reuse the same connection.

## Troubleshooting

**Image build fails**
- Ensure Docker daemon is running: `docker info`
- Check permissions: your user should be in the `docker` group or use rootless Docker

**Sandbox timeout**
- Increase `timeout` in `DockerSandbox` for long-running functions
- Default is 60 seconds per execution
