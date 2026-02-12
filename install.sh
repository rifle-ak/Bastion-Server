#!/usr/bin/env bash
# install.sh — One-command installer for Bastion Agent
#
# SSH into your bastion server and run:
#   curl -fsSL https://raw.githubusercontent.com/rifle-ak/Bastion-Server/main/install.sh | sudo bash
#
# Or if you prefer to inspect first:
#   curl -fsSL https://raw.githubusercontent.com/rifle-ak/Bastion-Server/main/install.sh -o install.sh
#   less install.sh
#   sudo bash install.sh
#
# Environment variables (optional):
#   ANTHROPIC_API_KEY  — set before running to auto-configure the API key
#   BRANCH             — git branch to install from (default: main)
#   INSTALL_DIR        — installation directory (default: /opt/bastion-agent)

set -euo pipefail

# ─── Configuration ──────────────────────────────────────────────────────────

REPO_URL="https://github.com/rifle-ak/Bastion-Server.git"
BRANCH="${BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/bastion-agent}"
AGENT_USER="claude-agent"
AGENT_HOME="/home/${AGENT_USER}"
SSH_KEY_DIR="${AGENT_HOME}/.ssh/keys"
CONFIG_DIR="/etc/bastion-agent"
LOG_DIR="/var/log/bastion-agent"
VENV_DIR="${INSTALL_DIR}/venv"

# ─── Colors ─────────────────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
RESET='\033[0m'

header()  { echo -e "\n${CYAN}${BOLD}$1${RESET}"; }
step()    { echo -e "  ${GREEN}[+]${RESET} $1"; }
skip()    { echo -e "  ${DIM}[*]${RESET} $1"; }
warn()    { echo -e "  ${YELLOW}[!]${RESET} $1"; }
fail()    { echo -e "  ${RED}[x]${RESET} $1"; exit 1; }

# ─── Preflight Checks ──────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║         Bastion Agent — Installer v0.1.0                ║${RESET}"
echo -e "${BOLD}║         Galaxy Gaming Host                              ║${RESET}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════════╝${RESET}"

header "Preflight Checks"

# Must be root
if [[ $EUID -ne 0 ]]; then
    fail "This script must be run as root (use sudo)"
fi

# Check OS
if [ -f /etc/os-release ]; then
    . /etc/os-release
    step "OS detected: ${PRETTY_NAME:-$ID}"
    if [[ "${ID:-}" != "ubuntu" && "${ID:-}" != "debian" ]]; then
        warn "This installer is designed for Ubuntu/Debian. Proceeding anyway..."
    fi
else
    warn "Could not detect OS. Proceeding..."
fi

# Check architecture
ARCH=$(uname -m)
step "Architecture: ${ARCH}"

# Check available memory
MEM_MB=$(free -m | awk '/Mem:/{print $2}')
step "Memory: ${MEM_MB}MB"
if [[ ${MEM_MB} -lt 512 ]]; then
    warn "Low memory (${MEM_MB}MB). Minimum 512MB recommended."
fi

# Check available disk
DISK_AVAIL=$(df -BM / | awk 'NR==2{print $4}' | tr -d 'M')
step "Disk available: ${DISK_AVAIL}MB"
if [[ ${DISK_AVAIL} -lt 500 ]]; then
    fail "Insufficient disk space. Need at least 500MB, have ${DISK_AVAIL}MB."
fi

# ─── 1. System Packages ────────────────────────────────────────────────────

header "1/8  Installing System Packages"

apt-get update -qq
apt-get install -y -qq \
    python3 \
    python3-pip \
    python3-venv \
    git \
    openssh-client \
    curl \
    jq \
    > /dev/null 2>&1

# Verify Python version
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [[ ${PYTHON_MAJOR} -lt 3 ]] || [[ ${PYTHON_MAJOR} -eq 3 && ${PYTHON_MINOR} -lt 11 ]]; then
    fail "Python 3.11+ required. Found: Python ${PYTHON_VERSION}"
fi

step "Python ${PYTHON_VERSION}"
step "git $(git --version | awk '{print $3}')"

# ─── 2. Agent User ─────────────────────────────────────────────────────────

header "2/8  Creating Agent User"

if ! id "${AGENT_USER}" &>/dev/null; then
    useradd --system --create-home --home-dir "${AGENT_HOME}" \
        --shell /bin/bash "${AGENT_USER}"
    step "Created user: ${AGENT_USER}"
else
    skip "User ${AGENT_USER} already exists"
fi

mkdir -p "${SSH_KEY_DIR}"
chown -R "${AGENT_USER}:${AGENT_USER}" "${AGENT_HOME}"
chmod 700 "${AGENT_HOME}/.ssh"
chmod 700 "${SSH_KEY_DIR}"
step "SSH key directory: ${SSH_KEY_DIR}"

# Add to docker group if available
if getent group docker &>/dev/null; then
    usermod -aG docker "${AGENT_USER}" 2>/dev/null || true
    step "Added to docker group"
fi

# ─── 3. Clone / Update Repository ──────────────────────────────────────────

header "3/8  Installing Bastion Agent"

if [ -d "${INSTALL_DIR}/.git" ]; then
    skip "Repository exists, pulling latest..."
    cd "${INSTALL_DIR}"
    git fetch origin "${BRANCH}" --quiet
    git checkout "${BRANCH}" --quiet
    git pull origin "${BRANCH}" --quiet
else
    if [ -d "${INSTALL_DIR}" ]; then
        rm -rf "${INSTALL_DIR}"
    fi
    git clone --branch "${BRANCH}" --depth 1 "${REPO_URL}" "${INSTALL_DIR}" --quiet
    step "Cloned from ${REPO_URL} (branch: ${BRANCH})"
fi

cd "${INSTALL_DIR}"

# ─── 4. Python Virtualenv + Dependencies ───────────────────────────────────

header "4/8  Setting Up Python Environment"

if [ ! -d "${VENV_DIR}" ]; then
    python3 -m venv "${VENV_DIR}"
    step "Created virtualenv: ${VENV_DIR}"
else
    skip "Virtualenv already exists"
fi

"${VENV_DIR}/bin/pip" install --quiet --upgrade pip setuptools wheel
step "Updated pip + setuptools"

"${VENV_DIR}/bin/pip" install --quiet -r requirements.txt
"${VENV_DIR}/bin/pip" install --quiet -e .
step "Installed bastion-agent + dependencies"

# Verify install
AGENT_VERSION=$("${VENV_DIR}/bin/bastion-agent" --version 2>&1 | tail -1)
step "Installed: ${AGENT_VERSION}"

# ─── 5. Configuration ──────────────────────────────────────────────────────

header "5/8  Configuring"

# Create system config directory
mkdir -p "${CONFIG_DIR}"

# Copy default configs if not already present
for f in agent.yaml servers.yaml permissions.yaml; do
    if [ ! -f "${CONFIG_DIR}/${f}" ]; then
        cp "${INSTALL_DIR}/config/${f}" "${CONFIG_DIR}/${f}"
        step "Installed config: ${CONFIG_DIR}/${f}"
    else
        skip "Config exists: ${CONFIG_DIR}/${f}"
    fi
done

# Create log directory
mkdir -p "${LOG_DIR}"
chown "${AGENT_USER}:${AGENT_USER}" "${LOG_DIR}"
chmod 750 "${LOG_DIR}"
step "Log directory: ${LOG_DIR}"

# Create env file for API key
mkdir -p "${CONFIG_DIR}"
if [ ! -f "${CONFIG_DIR}/env" ]; then
    if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
        echo "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}" > "${CONFIG_DIR}/env"
        chmod 600 "${CONFIG_DIR}/env"
        step "API key configured from environment"
    else
        cat > "${CONFIG_DIR}/env" << 'EOF'
# Bastion Agent environment variables
# Get your API key at: https://console.anthropic.com/settings/keys
ANTHROPIC_API_KEY=sk-ant-your-key-here
EOF
        chmod 600 "${CONFIG_DIR}/env"
        warn "API key placeholder created at ${CONFIG_DIR}/env — edit this!"
    fi
else
    skip "Environment file exists: ${CONFIG_DIR}/env"
fi

# Update agent.yaml to use system log path
sed -i "s|audit_log_path:.*|audit_log_path: ${LOG_DIR}/audit.jsonl|" "${CONFIG_DIR}/agent.yaml"

# ─── 6. Systemd Service ────────────────────────────────────────────────────

header "6/8  Installing Systemd Service"

# Write the service file with correct paths
cat > /etc/systemd/system/bastion-agent.service << UNIT
[Unit]
Description=Bastion Agent - Infrastructure Management for Galaxy Gaming Host
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${AGENT_USER}
Group=${AGENT_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${VENV_DIR}/bin/bastion-agent run --config-dir ${CONFIG_DIR}
Restart=on-failure
RestartSec=10

# Environment
EnvironmentFile=-${CONFIG_DIR}/env
Environment=BASTION_AGENT_LOG_LEVEL=INFO

# Security hardening
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=${LOG_DIR} ${INSTALL_DIR}/logs
PrivateTmp=yes

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=bastion-agent

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
step "Service installed: bastion-agent.service"

# ─── 7. CLI Launcher ──────────────────────────────────────────────────────

header "7/8  Installing CLI Launcher"

# Install the 'bastion' wrapper to /usr/local/bin
if [ -f "${INSTALL_DIR}/scripts/bastion" ]; then
    install -m 755 "${INSTALL_DIR}/scripts/bastion" /usr/local/bin/bastion
    step "Installed: /usr/local/bin/bastion"
else
    warn "scripts/bastion not found — skipping launcher install"
    warn "You may be on an older branch. Merge latest changes and re-run."
fi

# Add sudoers drop-in so staff can run 'bastion' without typing their password.
# The wrapper auto-elevates to root to read /etc/bastion-agent/env, then
# drops to claude-agent. This entry lets that auto-elevation be passwordless.
SUDOERS_FILE="/etc/sudoers.d/bastion-agent"
if [ ! -f "${SUDOERS_FILE}" ]; then
    cat > "${SUDOERS_FILE}" << 'SUDOERS'
# Allow members of the sudo group to launch the bastion agent without a password.
# The wrapper script (/usr/local/bin/bastion) reads the API key from the
# root-only env file, then drops privileges to the claude-agent user.
%sudo ALL=(root) NOPASSWD: /usr/local/bin/bastion
%sudo ALL=(root) NOPASSWD: /usr/local/bin/bastion *
SUDOERS
    chmod 440 "${SUDOERS_FILE}"
    step "Sudoers entry: staff can run 'bastion' without password"
else
    skip "Sudoers entry already exists"
fi

# ─── 8. SSH Hardening ──────────────────────────────────────────────────────

header "8/8  Hardening SSH"

SSHD_CONFIG="/etc/ssh/sshd_config"
SSHD_CHANGED=false

if [ -f "${SSHD_CONFIG}" ]; then
    # Only modify if not already hardened
    if grep -q "^PermitRootLogin yes" "${SSHD_CONFIG}" 2>/dev/null || \
       grep -q "^#PermitRootLogin" "${SSHD_CONFIG}" 2>/dev/null; then
        sed -i 's/^#*PermitRootLogin.*/PermitRootLogin no/' "${SSHD_CONFIG}"
        SSHD_CHANGED=true
    fi

    if grep -q "^PasswordAuthentication yes" "${SSHD_CONFIG}" 2>/dev/null || \
       grep -q "^#PasswordAuthentication" "${SSHD_CONFIG}" 2>/dev/null; then
        sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' "${SSHD_CONFIG}"
        SSHD_CHANGED=true
    fi

    if [ "$SSHD_CHANGED" = true ]; then
        step "SSH hardened: root login disabled, password auth disabled"
        warn "SSH config changed — restart sshd to apply: systemctl restart sshd"
    else
        skip "SSH already hardened"
    fi
fi

# UFW firewall
if command -v ufw &>/dev/null; then
    ufw allow ssh > /dev/null 2>&1
    ufw --force enable > /dev/null 2>&1
    step "UFW enabled (SSH allowed)"
fi

# ─── Done ───────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║                  Installation Complete                   ║${RESET}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════════╝${RESET}"
echo ""

# Status summary
echo -e "  ${DIM}Install dir:${RESET}    ${INSTALL_DIR}"
echo -e "  ${DIM}Config dir:${RESET}     ${CONFIG_DIR}"
echo -e "  ${DIM}Log dir:${RESET}        ${LOG_DIR}"
echo -e "  ${DIM}Agent user:${RESET}     ${AGENT_USER}"
echo -e "  ${DIM}SSH keys:${RESET}       ${SSH_KEY_DIR}"
echo -e "  ${DIM}Virtualenv:${RESET}     ${VENV_DIR}"
echo ""

# Check API key status
if grep -q "sk-ant-your-key-here" "${CONFIG_DIR}/env" 2>/dev/null; then
    echo -e "  ${RED}${BOLD}ACTION REQUIRED:${RESET} Set your Anthropic API key:"
    echo -e "    ${CYAN}sudo nano ${CONFIG_DIR}/env${RESET}"
    echo ""
fi

echo -e "  ${BOLD}Next Steps:${RESET}"
echo ""
echo -e "  ${CYAN}1.${RESET} Edit your server inventory:"
echo -e "     ${DIM}sudo nano ${CONFIG_DIR}/servers.yaml${RESET}"
echo ""
echo -e "  ${CYAN}2.${RESET} Generate SSH keys for downstream servers:"
echo -e "     ${DIM}cd ${INSTALL_DIR} && sudo bash scripts/generate-ssh-keys.sh${RESET}"
echo ""
echo -e "  ${CYAN}3.${RESET} Run interactively:"
echo -e "     ${DIM}bastion${RESET}"
echo ""
echo -e "  ${CYAN}4.${RESET} Or enable as a service:"
echo -e "     ${DIM}sudo systemctl enable --now bastion-agent${RESET}"
echo -e "     ${DIM}sudo journalctl -u bastion-agent -f${RESET}"
echo ""
