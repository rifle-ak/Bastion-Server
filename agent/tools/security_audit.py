"""Server security hardening audit.

Checks SSH configuration, open ports, firewall rules, file
permissions, outdated packages, and common security misconfigurations.
Produces a scored security report.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from agent.inventory import Inventory
from agent.tools.base import BaseTool, ToolResult
from agent.tools.docker_tools import _run_on_server


class SecurityAudit(BaseTool):
    """One-shot security hardening audit for a server."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "security_audit"

    @property
    def description(self) -> str:
        return (
            "Security hardening audit: SSH config, open ports, firewall, "
            "world-writable files, SUID binaries, failed logins, password "
            "auth, root login. Scores the server's security posture."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server to audit.",
                },
            },
            "required": ["server"],
        }

    async def execute(self, *, server: str, **kwargs: Any) -> ToolResult:
        """Run security audit."""
        checks: dict[str, Any] = {
            # SSH config
            "sshd_config": _run_on_server(
                self._inventory, server,
                "cat /etc/ssh/sshd_config 2>/dev/null",
            ),
            # Open ports
            "open_ports": _run_on_server(
                self._inventory, server, "ss -tlnp",
            ),
            # Firewall rules
            "iptables": _run_on_server(
                self._inventory, server, "iptables -nL 2>/dev/null",
            ),
            "nft": _run_on_server(
                self._inventory, server, "nft list ruleset 2>/dev/null",
            ),
            # Failed login attempts (last 50)
            "failed_logins": _run_on_server(
                self._inventory, server,
                "last -50 -f /var/log/btmp 2>/dev/null",
            ),
            # Users with login shells
            "login_users": _run_on_server(
                self._inventory, server,
                "cat /etc/passwd 2>/dev/null",
            ),
            # Kernel version
            "kernel": _run_on_server(
                self._inventory, server, "uname -r",
            ),
            # Automatic updates
            "auto_updates": _run_on_server(
                self._inventory, server,
                "systemctl is-active unattended-upgrades 2>/dev/null",
            ),
            # Pending security updates
            "updates": _run_on_server(
                self._inventory, server,
                "apt list --upgradable 2>/dev/null",
            ),
            "yum_updates": _run_on_server(
                self._inventory, server,
                "yum check-update --security 2>/dev/null",
            ),
            # World-writable files in sensitive locations
            "world_writable": _run_on_server(
                self._inventory, server,
                "find /etc /usr/local/bin /var/www -type f -perm -o+w 2>/dev/null",
            ),
            # SUID binaries (potential privilege escalation)
            "suid": _run_on_server(
                self._inventory, server,
                "find /usr -perm -4000 -type f 2>/dev/null",
            ),
            # Running as root
            "root_procs": _run_on_server(
                self._inventory, server,
                "ps aux --no-headers -U root 2>/dev/null",
            ),
        }

        keys = list(checks.keys())
        results = await asyncio.gather(*[checks[k] for k in keys])
        data = dict(zip(keys, results))

        return ToolResult(output=_build_security_report(server, data))


def _v(data: dict[str, ToolResult], key: str) -> str:
    r = data.get(key)
    return r.output.strip() if r and r.success else ""


def _build_security_report(server: str, data: dict[str, ToolResult]) -> str:
    """Build security audit report with scoring."""
    sections: list[str] = [f"# Security Audit: {server}\n"]
    score = 100
    findings: list[str] = []

    # ── SSH Configuration ──
    sections.append("## SSH Configuration")
    sshd = _v(data, "sshd_config")
    if sshd:
        # Password authentication
        if re.search(r'^\s*PasswordAuthentication\s+yes', sshd, re.MULTILINE | re.IGNORECASE):
            findings.append("✗ SSH PasswordAuthentication enabled — use key-only auth")
            score -= 15
        elif re.search(r'^\s*PasswordAuthentication\s+no', sshd, re.MULTILINE | re.IGNORECASE):
            sections.append("✓ Password auth disabled (key-only)")
        else:
            findings.append("⚠ PasswordAuthentication not explicitly set (defaults to yes)")
            score -= 5

        # Root login
        if re.search(r'^\s*PermitRootLogin\s+yes', sshd, re.MULTILINE | re.IGNORECASE):
            findings.append("✗ Root SSH login permitted — disable it")
            score -= 15
        elif re.search(r'^\s*PermitRootLogin\s+no', sshd, re.MULTILINE | re.IGNORECASE):
            sections.append("✓ Root login disabled")
        elif re.search(r'^\s*PermitRootLogin\s+prohibit-password', sshd, re.MULTILINE | re.IGNORECASE):
            sections.append("✓ Root login: key-only (prohibit-password)")

        # Port
        port_match = re.search(r'^\s*Port\s+(\d+)', sshd, re.MULTILINE)
        if port_match:
            port = port_match.group(1)
            if port == "22":
                findings.append("⚠ SSH on default port 22 — consider changing to reduce scan noise")
                score -= 3
            else:
                sections.append(f"✓ SSH on non-default port {port}")

        # Protocol
        if re.search(r'^\s*Protocol\s+1', sshd, re.MULTILINE):
            findings.append("✗ SSH Protocol 1 enabled — insecure, use Protocol 2 only")
            score -= 20

        # MaxAuthTries
        max_auth = re.search(r'^\s*MaxAuthTries\s+(\d+)', sshd, re.MULTILINE)
        if max_auth and int(max_auth.group(1)) > 6:
            findings.append(f"⚠ MaxAuthTries {max_auth.group(1)} — lower to 3-4")
            score -= 2

    # ── Open Ports ──
    sections.append("\n## Open Ports")
    ports = _v(data, "open_ports")
    if ports:
        port_lines = [l for l in ports.splitlines() if "LISTEN" in l]
        sections.append(f"{len(port_lines)} listening ports:")
        risky_ports = {
            "3306": "MySQL (should not be public)",
            "5432": "PostgreSQL (should not be public)",
            "6379": "Redis (should not be public)",
            "27017": "MongoDB (should not be public)",
            "11211": "Memcached (should not be public)",
            "9200": "Elasticsearch (should not be public)",
        }
        for line in port_lines:
            sections.append(f"  {line.strip()}")
            for rport, desc in risky_ports.items():
                if f":{rport}" in line and "127.0.0.1" not in line and "::1" not in line:
                    findings.append(f"✗ {desc} — port {rport} open on all interfaces")
                    score -= 10

    # ── Firewall ──
    sections.append("\n## Firewall")
    iptables = _v(data, "iptables")
    nft = _v(data, "nft")
    if iptables and "ACCEPT" in iptables and "DROP" in iptables:
        sections.append("✓ iptables active with rules")
    elif nft and "table" in nft:
        sections.append("✓ nftables active with rules")
    elif iptables and iptables.count("\n") < 5:
        findings.append("✗ No firewall rules — server is fully exposed")
        score -= 20
    else:
        findings.append("⚠ Could not verify firewall status")
        score -= 5

    # ── Failed Logins ──
    failed = _v(data, "failed_logins")
    if failed:
        failed_count = len([l for l in failed.splitlines() if l.strip() and "btmp" not in l])
        if failed_count > 20:
            findings.append(f"⚠ {failed_count} failed login attempts — consider fail2ban")
            score -= 5
        sections.append(f"\n**Failed logins:** {failed_count} recent attempts")

    # ── User Accounts ──
    users = _v(data, "login_users")
    if users:
        login_shells = ["/bin/bash", "/bin/sh", "/bin/zsh", "/usr/bin/zsh"]
        login_users = [
            l.split(":")[0] for l in users.splitlines()
            if any(l.endswith(sh) for sh in login_shells)
        ]
        sections.append(f"\n**Users with login shells:** {len(login_users)}")
        if len(login_users) > 10:
            findings.append(f"⚠ {len(login_users)} users with login shells — review and disable unused accounts")
            score -= 3

    # ── Updates ──
    sections.append("\n## System Updates")
    updates = _v(data, "updates") or _v(data, "yum_updates")
    if updates:
        update_count = len([l for l in updates.splitlines() if "upgradable" in l.lower() or "update" in l.lower()])
        if update_count > 10:
            findings.append(f"⚠ {update_count} pending updates — apply security patches")
            score -= 5
        elif update_count > 0:
            sections.append(f"{update_count} updates available")
        else:
            sections.append("✓ System up to date")

    auto = _v(data, "auto_updates")
    if auto == "active":
        sections.append("✓ Automatic updates enabled")
    else:
        findings.append("⚠ Automatic security updates not enabled")
        score -= 5

    # ── World-Writable Files ──
    world_writable = _v(data, "world_writable")
    if world_writable:
        ww_files = [l for l in world_writable.splitlines() if l.strip()]
        if ww_files:
            findings.append(f"✗ {len(ww_files)} world-writable files in sensitive directories")
            score -= 10
            for f in ww_files[:5]:
                sections.append(f"  {f}")

    # ── Kernel ──
    kernel = _v(data, "kernel")
    if kernel:
        sections.append(f"\n**Kernel:** {kernel}")

    # ── Score ──
    score = max(0, score)
    sections.append("\n---")
    sections.append(f"\n## Security Score: {score}/100\n")

    if score >= 80:
        grade = "A" if score >= 90 else "B"
        sections.append(f"**Grade: {grade}** — Good security posture")
    elif score >= 60:
        sections.append("**Grade: C** — Needs improvement")
    elif score >= 40:
        sections.append("**Grade: D** — Significant security gaps")
    else:
        sections.append("**Grade: F** — Critical security issues")

    if findings:
        sections.append(f"\n**{len(findings)} findings:**\n")
        critical = [f for f in findings if f.startswith("✗")]
        warnings = [f for f in findings if f.startswith("⚠")]
        for f in critical:
            sections.append(f)
        for f in warnings:
            sections.append(f)
    else:
        sections.append("\n✓ No security issues found.")

    return "\n".join(sections)
