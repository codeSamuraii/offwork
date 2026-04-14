#!/usr/bin/env bash
# setup_sandbox_macos.sh — One-command sandbox setup for pyfuse on Apple Silicon.
#
# What it does:
#   1. Installs tart (Virtualization.framework VM manager) via Homebrew.
#   2. Clones a lightweight Ubuntu VM image.
#   3. Generates a dedicated SSH key pair.
#   4. Boots the VM, installs Python and the guest agent, then stops it.
#
# After running this script the worker can be started with:
#   pyfuse worker --backend redis://... --sandbox
#
# Requirements:
#   - macOS on Apple Silicon (arm64)
#   - Homebrew installed
#
# Usage:
#   bash scripts/setup_sandbox_macos.sh
#
set -euo pipefail

# ---- Configuration --------------------------------------------------------

VM_NAME="${PYFUSE_SANDBOX_VM:-pyfuse-sandbox}"
GUEST_USER="pyfuse"
GUEST_PORT="${PYFUSE_SANDBOX_PORT:-9749}"
PYFUSE_DIR="$HOME/.pyfuse/sandbox"
SSH_KEY="$PYFUSE_DIR/id_ed25519"

# Tart base image (Ubuntu 24.04 LTS, arm64)
BASE_IMAGE="ghcr.io/cirruslabs/ubuntu:latest"

# ---- Helpers --------------------------------------------------------------

info()  { printf "\033[1;34m[pyfuse]\033[0m %s\n" "$*"; }
ok()    { printf "\033[1;32m[pyfuse]\033[0m %s\n" "$*"; }
warn()  { printf "\033[1;33m[pyfuse]\033[0m %s\n" "$*" >&2; }
die()   { printf "\033[1;31m[pyfuse]\033[0m %s\n" "$*" >&2; exit 1; }

require_macos_arm64() {
    [[ "$(uname -s)" == "Darwin" ]] || die "This script requires macOS."
    [[ "$(uname -m)" == "arm64" ]]  || die "This script requires Apple Silicon (arm64)."
}

require_homebrew() {
    command -v brew &>/dev/null || die "Homebrew is required. Install it from https://brew.sh"
}

# ---- Steps ----------------------------------------------------------------

install_tart() {
    if command -v tart &>/dev/null; then
        info "tart is already installed: $(tart --version 2>/dev/null || echo 'unknown version')"
        return
    fi
    info "Installing tart via Homebrew..."
    brew install cirruslabs/cli/tart
    ok "tart installed."
}

create_vm() {
    if tart list 2>/dev/null | grep -Fxq -- "$VM_NAME"; then
        info "VM '$VM_NAME' already exists, skipping creation."
        return
    fi
    info "Cloning base image $BASE_IMAGE → $VM_NAME ..."
    tart clone "$BASE_IMAGE" "$VM_NAME"
    ok "VM '$VM_NAME' created."
}

generate_ssh_key() {
    mkdir -p "$PYFUSE_DIR"
    if [[ -f "$SSH_KEY" ]]; then
        info "SSH key already exists at $SSH_KEY"
        return
    fi
    info "Generating SSH key pair..."
    ssh-keygen -t ed25519 -f "$SSH_KEY" -N "" -C "pyfuse-sandbox"
    ok "SSH key generated at $SSH_KEY"
}

wait_for_ip() {
    info "Waiting for VM to obtain an IP address..."
    for i in $(seq 1 60); do
        VM_IP=$(tart ip "$VM_NAME" 2>/dev/null || true)
        if [[ -n "$VM_IP" ]]; then
            ok "VM IP: $VM_IP"
            return
        fi
        sleep 1
    done
    die "VM did not obtain an IP within 60 seconds."
}

wait_for_ssh() {
    info "Waiting for SSH to become available..."
    for i in $(seq 1 30); do
        if ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
               -o ConnectTimeout=2 -o LogLevel=ERROR \
               -i "$SSH_KEY" "$GUEST_USER@$VM_IP" "true" 2>/dev/null; then
            ok "SSH is ready."
            return
        fi
        sleep 2
    done
    die "SSH did not become available within 60 seconds."
}

ssh_cmd() {
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o LogLevel=ERROR -i "$SSH_KEY" "$GUEST_USER@$VM_IP" "$@"
}

provision_vm() {
    info "Starting VM '$VM_NAME' for provisioning..."
    tart run "$VM_NAME" --no-graphics &
    TART_PID=$!
    trap "kill $TART_PID 2>/dev/null || true; wait $TART_PID 2>/dev/null || true" EXIT

    wait_for_ip

    # Set up SSH key-based auth
    info "Configuring SSH access..."
    PUB_KEY=$(cat "${SSH_KEY}.pub")

    # Try password-based login first to inject the SSH key.
    # The default tart Ubuntu image uses admin/admin.
    sshpass -p "admin" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o LogLevel=ERROR "admin@$VM_IP" bash -s <<SETUP_SSH
set -e
# Create pyfuse user if it doesn't exist
if ! id -u $GUEST_USER &>/dev/null; then
    sudo useradd -m -s /bin/bash $GUEST_USER
    echo "$GUEST_USER ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/$GUEST_USER
fi
sudo mkdir -p /home/$GUEST_USER/.ssh
echo "$PUB_KEY" | sudo tee /home/$GUEST_USER/.ssh/authorized_keys
sudo chown -R $GUEST_USER:$GUEST_USER /home/$GUEST_USER/.ssh
sudo chmod 700 /home/$GUEST_USER/.ssh
sudo chmod 600 /home/$GUEST_USER/.ssh/authorized_keys
SETUP_SSH

    wait_for_ssh

    # Install Python and dependencies
    info "Installing Python inside the VM..."
    ssh_cmd bash -s <<'PROVISION'
set -e
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-pip python3-venv > /dev/null 2>&1
python3 --version
PROVISION

    # Deploy the guest agent
    AGENT_SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/pyfuse/worker/sandbox/guest_agent.py"
    if [[ ! -f "$AGENT_SCRIPT" ]]; then
        # Fallback: try to find it via Python
        AGENT_SCRIPT=$(python3 -c "
import pyfuse.worker.sandbox.guest_agent as g
print(g.__file__)
" 2>/dev/null || true)
    fi

    if [[ -f "$AGENT_SCRIPT" ]]; then
        info "Deploying guest agent..."
        scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
            -o LogLevel=ERROR -i "$SSH_KEY" \
            "$AGENT_SCRIPT" "$GUEST_USER@$VM_IP:/home/$GUEST_USER/guest_agent.py"

        # Create a systemd service for the guest agent
        ssh_cmd bash -s <<AGENT_SERVICE
set -e
cat <<'EOF' | sudo tee /etc/systemd/system/pyfuse-agent.service
[Unit]
Description=pyfuse Sandbox Guest Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$GUEST_USER
ExecStart=/usr/bin/python3 /home/$GUEST_USER/guest_agent.py --port $GUEST_PORT
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable pyfuse-agent
sudo systemctl start pyfuse-agent
AGENT_SERVICE
        ok "Guest agent deployed and enabled."
    else
        warn "Guest agent script not found — you'll need to deploy it manually."
    fi

    info "Stopping VM after provisioning..."
    kill $TART_PID 2>/dev/null || true
    wait $TART_PID 2>/dev/null || true
    trap - EXIT

    ok "VM '$VM_NAME' provisioned successfully."
}

# ---- Main -----------------------------------------------------------------

main() {
    info "pyfuse sandbox setup for Apple Silicon"
    info "======================================="
    echo

    require_macos_arm64
    require_homebrew

    install_tart
    create_vm
    generate_ssh_key

    # Check if sshpass is available for provisioning
    if ! command -v sshpass &>/dev/null; then
        info "Installing sshpass for initial VM provisioning..."
        brew install sshpass 2>/dev/null || brew install esolitos/ipa/sshpass 2>/dev/null || {
            warn "sshpass not available. You may need to manually provision the VM."
            warn "See: https://github.com/codeSamuraii/pyfuse#sandbox-setup"
        }
    fi

    provision_vm

    echo
    ok "Setup complete! Start a sandboxed worker with:"
    ok "  pyfuse worker --backend redis://localhost:6379 --sandbox"
    echo
    ok "Configuration stored in: $PYFUSE_DIR"
    ok "  SSH key:  $SSH_KEY"
    ok "  VM name:  $VM_NAME"
    echo
    ok "Management commands:"
    ok "  pyfuse sandbox status    — check VM status"
    ok "  pyfuse sandbox teardown  — delete the VM"
}

main "$@"
