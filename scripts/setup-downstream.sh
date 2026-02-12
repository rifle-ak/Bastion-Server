#!/usr/bin/env bash
# setup-downstream.sh — Prepare a downstream server for bastion agent access
#
# Run this on the downstream server (or via SSH from the bastion).
# Usage: sudo bash scripts/setup-downstream.sh
#
# This creates the agent user and authorizes the bastion's SSH key.

set -euo pipefail

AGENT_USER="claude-agent"
AGENT_HOME="/home/${AGENT_USER}"

echo "=== Downstream Server Setup ==="

# 1. Create the agent user
if ! id "${AGENT_USER}" &>/dev/null; then
    echo "[+] Creating user: ${AGENT_USER}"
    useradd --create-home --home-dir "${AGENT_HOME}" \
        --shell /bin/bash "${AGENT_USER}"
else
    echo "[*] User ${AGENT_USER} already exists"
fi

# 2. Set up SSH directory
echo "[+] Setting up SSH directory"
mkdir -p "${AGENT_HOME}/.ssh"
chmod 700 "${AGENT_HOME}/.ssh"
touch "${AGENT_HOME}/.ssh/authorized_keys"
chmod 600 "${AGENT_HOME}/.ssh/authorized_keys"
chown -R "${AGENT_USER}:${AGENT_USER}" "${AGENT_HOME}/.ssh"

# 3. Add the agent user to required groups
echo "[+] Adding to groups"
# Docker group (if docker is installed)
if getent group docker &>/dev/null; then
    usermod -aG docker "${AGENT_USER}"
    echo "[+] Added to docker group"
fi

# systemd-journal (for journalctl access)
if getent group systemd-journal &>/dev/null; then
    usermod -aG systemd-journal "${AGENT_USER}"
    echo "[+] Added to systemd-journal group"
fi

# adm group (for /var/log access)
if getent group adm &>/dev/null; then
    usermod -aG adm "${AGENT_USER}"
    echo "[+] Added to adm group"
fi

# 4. Grant limited sudo (read-only commands only)
SUDOERS_FILE="/etc/sudoers.d/bastion-agent"
echo "[+] Setting up limited sudoers"
cat > "${SUDOERS_FILE}" << 'SUDOERS'
# Bastion agent — limited read-only sudo access
# No password required for these specific commands only
claude-agent ALL=(ALL) NOPASSWD: /usr/bin/systemctl status *
claude-agent ALL=(ALL) NOPASSWD: /usr/bin/journalctl *
SUDOERS
chmod 440 "${SUDOERS_FILE}"

echo ""
echo "=== Downstream Setup Complete ==="
echo "Next steps:"
echo "  1. Copy the bastion's public key to ${AGENT_HOME}/.ssh/authorized_keys"
echo "     From bastion: ssh-copy-id -i ~/.ssh/keys/<hostname>_ed25519 ${AGENT_USER}@<this-host>"
echo "  2. Test connectivity from the bastion"
