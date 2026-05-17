#!/usr/bin/env bash
# setup_sandbox_docker.sh — Build the pyfuse Docker sandbox image.
#
# What it does:
#   1. Detects a Docker-compatible runtime (Docker, colima, Podman …).
#   2. Builds the pyfuse-sandbox Docker image from the bundled Dockerfile.
#
# Supported platforms:
#   - Linux  (Docker Engine, Podman, or any OCI-compatible runtime)
#   - macOS  (Docker Desktop, colima, OrbStack, Rancher Desktop …)
#
# After running this script the worker can be started with:
#   pyfuse worker --backend redis://... --sandbox
#
# Usage:
#   bash scripts/setup_sandbox_docker.sh
#
set -euo pipefail

# ---- Configuration --------------------------------------------------------

IMAGE_NAME="${PYFUSE_SANDBOX_DOCKER_IMAGE:-pyfuse-sandbox}"

# ---- Helpers --------------------------------------------------------------

info()  { printf "\033[1;34m[pyfuse]\033[0m %s\n" "$*"; }
ok()    { printf "\033[1;32m[pyfuse]\033[0m %s\n" "$*"; }
warn()  { printf "\033[1;33m[pyfuse]\033[0m %s\n" "$*" >&2; }
die()   { printf "\033[1;31m[pyfuse]\033[0m %s\n" "$*" >&2; exit 1; }

# ---- Checks ---------------------------------------------------------------

check_docker() {
    if ! command -v docker &>/dev/null; then
        die "Docker CLI not found.
  Linux:  https://docs.docker.com/engine/install/
  macOS:  brew install --cask docker   OR   brew install colima && colima start"
    fi

    if ! docker info &>/dev/null 2>&1; then
        # Provide helpful hints for common runtimes
        if [[ "$(uname -s)" == "Darwin" ]]; then
            if command -v colima &>/dev/null; then
                die "Docker daemon not reachable. Try:  colima start"
            fi
            die "Docker daemon not reachable. Start Docker Desktop or run:  colima start"
        else
            die "Docker daemon not reachable. Ensure the Docker service is running:
  sudo systemctl start docker"
        fi
    fi

    local runtime
    runtime=$(docker info --format '{{.Name}}' 2>/dev/null || echo "unknown")
    ok "Docker runtime detected: $runtime"
}

# ---- Build -----------------------------------------------------------------

build_image() {
    # Locate the Dockerfile directory (pyfuse/worker/sandbox/)
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
    DOCKERFILE_DIR="$REPO_ROOT/pyfuse/worker/sandbox"

    if [[ ! -f "$DOCKERFILE_DIR/Dockerfile" ]]; then
        # Fallback: try to find it via Python
        DOCKERFILE_DIR=$(python3 -c "
from pathlib import Path
import pyfuse.worker.sandbox as s
print(Path(s.__file__).resolve().parent)
" 2>/dev/null || true)
    fi

    if [[ ! -f "$DOCKERFILE_DIR/Dockerfile" ]]; then
        die "Dockerfile not found. Make sure pyfuse is installed."
    fi

    info "Building Docker image '$IMAGE_NAME' from $DOCKERFILE_DIR ..."
    docker build -t "$IMAGE_NAME" "$DOCKERFILE_DIR"
    ok "Image '$IMAGE_NAME' built successfully."
}

# ---- Main -----------------------------------------------------------------

main() {
    info "pyfuse sandbox setup"
    info "===================="
    echo

    check_docker
    build_image

    echo
    ok "Setup complete! Start a sandboxed worker with:"
    ok "  pyfuse worker --backend redis://localhost:6379 --sandbox"
    echo
    ok "Management commands:"
    ok "  pyfuse sandbox status     — check sandbox status"
    ok "  pyfuse sandbox teardown   — remove the Docker sandbox"
}

main "$@"