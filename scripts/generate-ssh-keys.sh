#!/usr/bin/env bash
# generate-ssh-keys.sh — Generate per-host SSH keypairs for the bastion agent
#
# Run on the bastion server as the agent user (or root).
# Usage: bash scripts/generate-ssh-keys.sh [server-name ...]
#
# If no server names given, reads from config/servers.yaml.

set -euo pipefail

AGENT_USER="claude-agent"
KEY_DIR="/home/${AGENT_USER}/.ssh/keys"

echo "=== SSH Key Generation ==="

# Create key directory
mkdir -p "${KEY_DIR}"

# Get server list
if [ $# -gt 0 ]; then
    SERVERS=("$@")
else
    # Try to parse server names from servers.yaml
    if [ -f "config/servers.yaml" ]; then
        SERVERS=($(python3 -c "
import yaml
with open('config/servers.yaml') as f:
    data = yaml.safe_load(f)
for name, srv in data.get('servers', {}).items():
    if srv.get('ssh', True) and name != 'localhost':
        print(name)
" 2>/dev/null || true))
    fi

    if [ ${#SERVERS[@]} -eq 0 ]; then
        echo "Usage: $0 <server-name> [server-name ...]"
        echo "Or ensure config/servers.yaml exists with server definitions."
        exit 1
    fi
fi

for SERVER in "${SERVERS[@]}"; do
    KEY_FILE="${KEY_DIR}/${SERVER}_ed25519"

    if [ -f "${KEY_FILE}" ]; then
        echo "[*] Key already exists: ${KEY_FILE}"
        continue
    fi

    echo "[+] Generating Ed25519 key for: ${SERVER}"
    ssh-keygen -t ed25519 -f "${KEY_FILE}" -N "" -C "bastion-agent@${SERVER}"
    echo "[+] Created: ${KEY_FILE}"
done

# Fix ownership
chown -R "${AGENT_USER}:${AGENT_USER}" "${KEY_DIR}" 2>/dev/null || true
chmod 600 "${KEY_DIR}"/*_ed25519 2>/dev/null || true
chmod 644 "${KEY_DIR}"/*_ed25519.pub 2>/dev/null || true

echo ""
echo "=== Key Generation Complete ==="
echo "Keys are in: ${KEY_DIR}"
echo ""
echo "Next: copy public keys to downstream servers:"
for SERVER in "${SERVERS[@]}"; do
    echo "  ssh-copy-id -i ${KEY_DIR}/${SERVER}_ed25519 ${AGENT_USER}@<${SERVER}-host>"
done
