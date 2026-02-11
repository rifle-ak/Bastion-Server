#!/usr/bin/env bash
# setup-bastion.sh — Harden the bastion server and prepare for bastion-agent
#
# Run as root on the bastion server.
# Usage: sudo bash scripts/setup-bastion.sh

set -euo pipefail

AGENT_USER="claude-agent"
AGENT_HOME="/home/${AGENT_USER}"
SSH_KEY_DIR="${AGENT_HOME}/.ssh/keys"
INSTALL_DIR="/opt/bastion-agent"
LOG_DIR="/var/log/bastion-agent"

echo "=== Bastion Server Setup ==="

# 1. Create the agent user (no login shell, restricted)
if ! id "${AGENT_USER}" &>/dev/null; then
    echo "[+] Creating user: ${AGENT_USER}"
    useradd --system --create-home --home-dir "${AGENT_HOME}" \
        --shell /usr/sbin/nologin "${AGENT_USER}"
else
    echo "[*] User ${AGENT_USER} already exists"
fi

# 2. Create directory structure
echo "[+] Creating directories"
mkdir -p "${SSH_KEY_DIR}"
mkdir -p "${INSTALL_DIR}"
mkdir -p "${LOG_DIR}"

# 3. Set permissions
echo "[+] Setting permissions"
chown -R "${AGENT_USER}:${AGENT_USER}" "${AGENT_HOME}"
chmod 700 "${AGENT_HOME}/.ssh"
chmod 700 "${SSH_KEY_DIR}"
chown -R "${AGENT_USER}:${AGENT_USER}" "${LOG_DIR}"
chmod 750 "${LOG_DIR}"

# 4. Install Python dependencies
echo "[+] Installing system packages"
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv

# 5. Create virtualenv and install agent
echo "[+] Setting up virtualenv"
python3 -m venv "${INSTALL_DIR}/venv"
"${INSTALL_DIR}/venv/bin/pip" install --quiet --upgrade pip

if [ -f "requirements.txt" ]; then
    "${INSTALL_DIR}/venv/bin/pip" install --quiet -r requirements.txt
    "${INSTALL_DIR}/venv/bin/pip" install --quiet -e .
    echo "[+] Agent installed"
else
    echo "[!] No requirements.txt found — run from the project directory"
fi

# 6. Firewall hardening (if ufw is available)
if command -v ufw &>/dev/null; then
    echo "[+] Configuring firewall"
    ufw default deny incoming
    ufw default allow outgoing
    ufw allow ssh
    ufw --force enable
    echo "[+] UFW enabled: deny incoming, allow outgoing, allow SSH"
else
    echo "[*] UFW not installed, skipping firewall setup"
fi

# 7. SSH hardening
echo "[+] Hardening SSH config"
SSHD_CONFIG="/etc/ssh/sshd_config"
if [ -f "${SSHD_CONFIG}" ]; then
    # Disable root login and password auth (if not already done)
    sed -i 's/^#*PermitRootLogin.*/PermitRootLogin no/' "${SSHD_CONFIG}"
    sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' "${SSHD_CONFIG}"
    sed -i 's/^#*ChallengeResponseAuthentication.*/ChallengeResponseAuthentication no/' "${SSHD_CONFIG}"
    echo "[+] SSH hardened: root login disabled, password auth disabled"
fi

# 8. Install systemd service
if [ -f "systemd/bastion-agent.service" ]; then
    echo "[+] Installing systemd service"
    cp systemd/bastion-agent.service /etc/systemd/system/
    systemctl daemon-reload
    echo "[+] Service installed. Enable with: systemctl enable bastion-agent"
fi

echo ""
echo "=== Setup Complete ==="
echo "Next steps:"
echo "  1. Generate SSH keys: bash scripts/generate-ssh-keys.sh"
echo "  2. Set up downstream servers: bash scripts/setup-downstream.sh <host>"
echo "  3. Set ANTHROPIC_API_KEY in /etc/bastion-agent/env"
echo "  4. Start the agent: systemctl start bastion-agent"
