#!/usr/bin/env bash
# setup_sandbox_docker.sh — Build the pyfuse Docker sandbox image.
#
# What it does:
#   1. Checks that Docker is installed and running.
#   2. Builds the pyfuse-sandbox Docker image from the bundled Dockerfile.
#
# After running this script the worker can be started with:
#   pyfuse worker --backend redis://... --sandbox docker
#
# Requirements:
#   - Docker installed and running
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
    command -v docker &>/dev/null || die "Docker is not installed. Get it from https://docs.docker.com/get-docker/"
    docker info &>/dev/null || die "Docker daemon is not running. Please start Docker."
    ok "Docker is installed and running."
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
    info "pyfuse Docker sandbox setup"
    info "============================"
    echo

    check_docker
    build_image

    echo
    ok "Setup complete! Start a sandboxed worker with:"
    ok "  pyfuse worker --backend redis://localhost:6379 --sandbox docker"
    echo
    ok "Management commands:"
    ok "  pyfuse sandbox status              — check sandbox status"
    ok "  pyfuse sandbox teardown --docker   — remove the Docker sandbox"
}

main "$@"
