#!/usr/bin/env bash
# update.sh — Update bastion-agent without git
#
# Downloads the latest code from GitHub as a tarball, replaces the agent
# code, installs dependencies, and restarts the service.
#
# Works on servers where .git is missing (common after install.sh deploys
# with --depth 1 and the .git dir gets cleaned up or never used).
#
# Usage:
#   sudo bash /opt/bastion-agent/scripts/update.sh
#   sudo bash /opt/bastion-agent/scripts/update.sh dev     # update from a branch
#
# Or fetch and run directly:
#   curl -fsSL https://raw.githubusercontent.com/rifle-ak/Bastion-Server/main/scripts/update.sh | sudo bash

set -euo pipefail

# ─── Configuration ──────────────────────────────────────────────────────────

REPO_OWNER="rifle-ak"
REPO_NAME="Bastion-Server"
BRANCH="${1:-main}"
INSTALL_DIR="${BASTION_INSTALL_DIR:-/opt/bastion-agent}"
VENV_DIR="${INSTALL_DIR}/venv"
CONFIG_DIR="/etc/bastion-agent"
TARBALL_URL="https://github.com/${REPO_OWNER}/${REPO_NAME}/archive/refs/heads/${BRANCH}.tar.gz"

# ─── Colors ─────────────────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
RESET='\033[0m'

step()    { echo -e "  ${GREEN}[+]${RESET} $1"; }
warn()    { echo -e "  ${YELLOW}[!]${RESET} $1"; }
fail()    { echo -e "  ${RED}[x]${RESET} $1"; exit 1; }

# ─── Preflight ──────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║         Bastion Agent — Updater                         ║${RESET}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════════╝${RESET}"
echo ""

# Must be root
if [[ $EUID -ne 0 ]]; then
    fail "This script must be run as root (use sudo)"
fi

# Install dir must exist
if [[ ! -d "${INSTALL_DIR}" ]]; then
    fail "Install directory not found: ${INSTALL_DIR}"
fi

# Record current version
CURRENT_VERSION="unknown"
if [[ -f "${INSTALL_DIR}/agent/__init__.py" ]]; then
    CURRENT_VERSION=$(grep '__version__' "${INSTALL_DIR}/agent/__init__.py" | head -1 | cut -d'"' -f2 || echo "unknown")
fi
step "Current version: ${CURRENT_VERSION}"
step "Branch: ${BRANCH}"
step "Install dir: ${INSTALL_DIR}"

# ─── Download ───────────────────────────────────────────────────────────────

echo ""
step "Downloading from ${TARBALL_URL}"

TMPDIR=$(mktemp -d -t bastion-update-XXXXXX)
trap 'rm -rf "${TMPDIR}"' EXIT

TAR_PATH="${TMPDIR}/update.tar.gz"
curl -sfL --max-time 60 -o "${TAR_PATH}" "${TARBALL_URL}" \
    || fail "Download failed. Check your internet connection and branch name."

TAR_SIZE=$(stat -c%s "${TAR_PATH}" 2>/dev/null || stat -f%z "${TAR_PATH}" 2>/dev/null || echo 0)
if [[ "${TAR_SIZE}" -lt 1024 ]]; then
    fail "Downloaded file is too small (${TAR_SIZE} bytes) — likely a 404."
fi
step "Downloaded: $((TAR_SIZE / 1024))KB"

# ─── Extract ────────────────────────────────────────────────────────────────

EXTRACT_DIR="${TMPDIR}/extracted"
mkdir -p "${EXTRACT_DIR}"
tar -xzf "${TAR_PATH}" -C "${EXTRACT_DIR}" \
    || fail "Failed to extract tarball."

# GitHub tarballs extract to <RepoName>-<branch>/
SOURCE_DIR=$(find "${EXTRACT_DIR}" -mindepth 1 -maxdepth 1 -type d | head -1)
if [[ -z "${SOURCE_DIR}" || ! -f "${SOURCE_DIR}/agent/__init__.py" ]]; then
    fail "Extracted archive doesn't look like bastion-agent."
fi

NEW_VERSION=$(grep '__version__' "${SOURCE_DIR}/agent/__init__.py" | head -1 | cut -d'"' -f2 || echo "unknown")
step "New version: ${NEW_VERSION}"

# ─── Stop Service ───────────────────────────────────────────────────────────

SERVICE_WAS_RUNNING=false
if systemctl is-active --quiet bastion-agent 2>/dev/null; then
    SERVICE_WAS_RUNNING=true
    step "Stopping bastion-agent service..."
    systemctl stop bastion-agent
fi

# ─── Replace Code ───────────────────────────────────────────────────────────

echo ""
step "Replacing code files..."

# Replace agent/ directory
rm -rf "${INSTALL_DIR}/agent"
cp -r "${SOURCE_DIR}/agent" "${INSTALL_DIR}/agent"
step "Updated agent/"

# Replace tests/, scripts/, systemd/ if present
for dirname in tests scripts systemd; do
    if [[ -d "${SOURCE_DIR}/${dirname}" ]]; then
        rm -rf "${INSTALL_DIR}/${dirname}"
        cp -r "${SOURCE_DIR}/${dirname}" "${INSTALL_DIR}/${dirname}"
        step "Updated ${dirname}/"
    fi
done

# Replace top-level files
for fname in pyproject.toml requirements.txt install.sh README.md CLAUDE.md; do
    if [[ -f "${SOURCE_DIR}/${fname}" ]]; then
        cp "${SOURCE_DIR}/${fname}" "${INSTALL_DIR}/${fname}"
        step "Updated ${fname}"
    fi
done

# Make scripts executable
chmod +x "${INSTALL_DIR}/scripts/"*.sh 2>/dev/null || true
chmod +x "${INSTALL_DIR}/install.sh" 2>/dev/null || true

# ─── Install Dependencies ──────────────────────────────────────────────────

echo ""
PIP="${VENV_DIR}/bin/pip"
if [[ ! -f "${PIP}" ]]; then
    fail "pip not found at ${PIP}. Recreate the virtualenv: python3 -m venv ${VENV_DIR}"
fi

step "Installing dependencies..."
"${PIP}" install --quiet --upgrade pip setuptools wheel
"${PIP}" install --quiet -r "${INSTALL_DIR}/requirements.txt"
"${PIP}" install --quiet -e "${INSTALL_DIR}"
step "Dependencies installed."

# Verify
INSTALLED_VERSION=$("${VENV_DIR}/bin/bastion-agent" --version 2>&1 | tail -1)
step "Installed: ${INSTALLED_VERSION}"

# ─── Update CLI Launcher ───────────────────────────────────────────────────

if [[ -f "${INSTALL_DIR}/scripts/bastion" ]]; then
    install -m 755 "${INSTALL_DIR}/scripts/bastion" /usr/local/bin/bastion
    step "Updated /usr/local/bin/bastion launcher"
fi

# ─── Restart Service ────────────────────────────────────────────────────────

echo ""
if [[ "${SERVICE_WAS_RUNNING}" == true ]]; then
    step "Restarting bastion-agent service..."
    systemctl start bastion-agent
    sleep 1
    if systemctl is-active --quiet bastion-agent; then
        step "Service restarted successfully."
    else
        warn "Service failed to start. Check: journalctl -u bastion-agent -n 30 --no-pager"
    fi
else
    warn "Service was not running — skipping restart."
    echo -e "  ${DIM}Start with: sudo systemctl start bastion-agent${RESET}"
fi

# ─── Done ───────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║                  Update Complete                         ║${RESET}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════════╝${RESET}"
echo ""
echo -e "  ${DIM}Previous:${RESET}  ${CURRENT_VERSION}"
echo -e "  ${DIM}Current:${RESET}   ${NEW_VERSION}"
echo -e "  ${DIM}Branch:${RESET}    ${BRANCH}"
echo ""
echo -e "  ${DIM}Config files in ${CONFIG_DIR}/ were NOT modified.${RESET}"
echo -e "  ${DIM}Edit server inventory: sudo nano ${CONFIG_DIR}/servers.yaml${RESET}"
echo ""
