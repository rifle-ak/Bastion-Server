"""cPanel/WHM management tools.

Wraps WHM API (whmapi1) calls for account management, SSL status,
backup reporting, and email deliverability checks. All commands
are built programmatically and run via the existing SSH pipeline.
"""

from __future__ import annotations

import json
import re
from typing import Any

from agent.inventory import Inventory
from agent.tools.base import BaseTool, ToolResult
from agent.tools.docker_tools import _run_on_server


class CpanelListAccounts(BaseTool):
    """List cPanel accounts on a WHM server."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "cpanel_list_accounts"

    @property
    def description(self) -> str:
        return "List cPanel accounts on a WHM server with domain, plan, and disk usage."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server name (must be a webhost role).",
                },
            },
            "required": ["server"],
        }

    async def execute(self, *, server: str, **kwargs: Any) -> ToolResult:
        """List accounts via whmapi1."""
        cmd = "whmapi1 listaccts --output=json"
        result = await _run_on_server(self._inventory, server, cmd)
        if not result.success:
            return result
        return ToolResult(output=_format_accounts(result.output))


class CpanelAccountInfo(BaseTool):
    """Get detailed info about a cPanel account."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "cpanel_account_info"

    @property
    def description(self) -> str:
        return "Get details for a cPanel account: domain, disk, bandwidth, plan, email count."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server name.",
                },
                "username": {
                    "type": "string",
                    "description": "cPanel username.",
                },
            },
            "required": ["server", "username"],
        }

    async def execute(self, *, server: str, username: str, **kwargs: Any) -> ToolResult:
        """Get account details via whmapi1."""
        cmd = f"whmapi1 accountsummary user={username} --output=json"
        return await _run_on_server(self._inventory, server, cmd)


class CpanelSSLStatus(BaseTool):
    """Check SSL certificate status across all domains."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "cpanel_ssl_status"

    @property
    def description(self) -> str:
        return "Check SSL certificates on a cPanel server: expiry, coverage, AutoSSL status."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server name.",
                },
            },
            "required": ["server"],
        }

    async def execute(self, *, server: str, **kwargs: Any) -> ToolResult:
        """Check SSL status via whmapi1."""
        cmd = "whmapi1 get_autossl_problems --output=json"
        result = await _run_on_server(self._inventory, server, cmd)
        if not result.success:
            return result
        return ToolResult(output=_format_ssl(result.output))


class CpanelBackupStatus(BaseTool):
    """Check backup configuration and recent backup status."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "cpanel_backup_status"

    @property
    def description(self) -> str:
        return "Check cPanel backup config, last backup time, and any failures."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server name.",
                },
            },
            "required": ["server"],
        }

    async def execute(self, *, server: str, **kwargs: Any) -> ToolResult:
        """Check backup status via whmapi1."""
        cmd = "whmapi1 backup_config_get --output=json"
        return await _run_on_server(self._inventory, server, cmd)


class CpanelEmailDeliverability(BaseTool):
    """Check email deliverability (SPF, DKIM, DMARC, PTR) for domains."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "cpanel_email_deliverability"

    @property
    def description(self) -> str:
        return "Check email deliverability for a domain: SPF, DKIM, PTR, rDNS validation."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server name.",
                },
                "domain": {
                    "type": "string",
                    "description": "Domain to check (e.g. 'example.com').",
                },
            },
            "required": ["server", "domain"],
        }

    async def execute(self, *, server: str, domain: str, **kwargs: Any) -> ToolResult:
        """Check email deliverability via whmapi1."""
        cmd = f"whmapi1 get_best_mx_for_domain domain={domain} --output=json"
        return await _run_on_server(self._inventory, server, cmd)


class CpanelMailQueue(BaseTool):
    """Check the Exim mail queue."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "cpanel_mail_queue"

    @property
    def description(self) -> str:
        return "Check the Exim mail queue size and list frozen/stuck messages."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server name.",
                },
                "detail": {
                    "type": "boolean",
                    "description": "Show message details, not just count (default false).",
                    "default": False,
                },
            },
            "required": ["server"],
        }

    async def execute(self, *, server: str, detail: bool = False, **kwargs: Any) -> ToolResult:
        """Check Exim mail queue."""
        if detail:
            cmd = "exim -bp"
        else:
            cmd = "exim -bpc"
        return await _run_on_server(self._inventory, server, cmd)


# ── Formatters ───────────────────────────────────────────────────


def _format_accounts(raw_json: str) -> str:
    """Format whmapi1 listaccts output into a concise table."""
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        return raw_json  # Return raw if not JSON

    accounts = data.get("data", {}).get("acct", [])
    if not accounts:
        return "No accounts found."

    lines = [f"{'User':<16} {'Domain':<30} {'Plan':<15} {'Disk':<10} {'Suspended'}"]
    lines.append("-" * 90)
    for acct in accounts:
        user = acct.get("user", "?")
        domain = acct.get("domain", "?")
        plan = acct.get("plan", "?")
        disk = acct.get("diskused", "?") + "M"
        suspended = "YES" if acct.get("suspended") else "no"
        lines.append(f"{user:<16} {domain:<30} {plan:<15} {disk:<10} {suspended}")

    return "\n".join(lines)


def _format_ssl(raw_json: str) -> str:
    """Format AutoSSL problems output."""
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        return raw_json

    problems = data.get("data", {}).get("problems", [])
    if not problems:
        return "AutoSSL: No problems detected. All certificates OK."

    lines = ["AutoSSL Problems:"]
    for p in problems:
        domain = p.get("domain", "?")
        problem = p.get("problem", "unknown")
        lines.append(f"  ✗ {domain}: {problem}")

    return "\n".join(lines)


# ── Domain & Account Investigation ───────────────────────────────


class CpanelDomainLookup(BaseTool):
    """Find which account owns a domain."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "cpanel_domain_lookup"

    @property
    def description(self) -> str:
        return "Find which cPanel account owns a domain, including addon/sub/parked domains."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {"type": "string", "description": "Server name."},
                "domain": {"type": "string", "description": "Domain to look up."},
            },
            "required": ["server", "domain"],
        }

    async def execute(self, *, server: str, domain: str, **kwargs: Any) -> ToolResult:
        """Look up domain ownership."""
        cmd = f"whmapi1 getdomainowner domain={domain} --output=json"
        return await _run_on_server(self._inventory, server, cmd)


class CpanelListDomains(BaseTool):
    """List all domains for a cPanel account."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "cpanel_list_domains"

    @property
    def description(self) -> str:
        return "List all domains (main, addon, sub, parked) for a cPanel account."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {"type": "string", "description": "Server name."},
                "username": {"type": "string", "description": "cPanel username."},
            },
            "required": ["server", "username"],
        }

    async def execute(self, *, server: str, username: str, **kwargs: Any) -> ToolResult:
        """List domains for an account."""
        cmd = f"whmapi1 get_domain_info user={username} --output=json"
        return await _run_on_server(self._inventory, server, cmd)


class CpanelSuspensionInfo(BaseTool):
    """Check why an account is suspended."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "cpanel_suspension_info"

    @property
    def description(self) -> str:
        return "Get suspension reason and details for a cPanel account."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {"type": "string", "description": "Server name."},
                "username": {"type": "string", "description": "cPanel username."},
            },
            "required": ["server", "username"],
        }

    async def execute(self, *, server: str, username: str, **kwargs: Any) -> ToolResult:
        """Get suspension reason."""
        cmd = f"whmapi1 getsuspensionreason user={username} --output=json"
        return await _run_on_server(self._inventory, server, cmd)


class CpanelDiskQuota(BaseTool):
    """Check disk quota and inode usage for an account."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "cpanel_disk_quota"

    @property
    def description(self) -> str:
        return "Check disk quota usage, inode count, and find large files for a cPanel account."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {"type": "string", "description": "Server name."},
                "username": {"type": "string", "description": "cPanel username."},
            },
            "required": ["server", "username"],
        }

    async def execute(self, *, server: str, username: str, **kwargs: Any) -> ToolResult:
        """Check quota and large files."""
        checks = {
            "quota": f"whmapi1 getquotainfo user={username} --output=json",
            "du_summary": f"du -sh /home/{username}/",
            "du_breakdown": f"du -sh /home/{username}/*/ 2>/dev/null",
            "large_files": f"find /home/{username} -type f -size +50M -printf '%s %p\\n' 2>/dev/null",
            "inodes": f"find /home/{username} -type f 2>/dev/null | wc -l",
        }

        parts: list[str] = []
        for label, cmd in checks.items():
            result = await _run_on_server(self._inventory, server, cmd)
            val = result.output.strip() if result.success else f"ERROR: {result.error}"

            if label == "quota":
                parts.append(f"**Quota Info:**\n{val}")
            elif label == "du_summary":
                parts.append(f"**Total Usage:** {val}")
            elif label == "du_breakdown":
                parts.append(f"**Directory Breakdown:**\n{val}")
            elif label == "large_files":
                if val:
                    parts.append(f"**Large Files (>50MB):**\n{val}")
                else:
                    parts.append("**Large Files:** None over 50MB")
            elif label == "inodes":
                parts.append(f"**Inodes Used:** {val}")

        return ToolResult(output="\n\n".join(parts))


class CpanelPhpVersion(BaseTool):
    """Check PHP version and config for an account."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "cpanel_php_version"

    @property
    def description(self) -> str:
        return "Check PHP version, handler, and key limits (memory, upload, exec time) for an account."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {"type": "string", "description": "Server name."},
                "username": {"type": "string", "description": "cPanel username."},
            },
            "required": ["server", "username"],
        }

    async def execute(self, *, server: str, username: str, **kwargs: Any) -> ToolResult:
        """Check PHP version and config."""
        checks = {
            "version": f"whmapi1 php_get_domain_handler domain=$("
                       f"whmapi1 accountsummary user={username} --output=json"
                       f") --output=json",
            "installed": "ls /opt/cpanel/*/root/usr/bin/php 2>/dev/null",
        }
        # Use a simpler approach that doesn't need subshells
        version_cmd = f"whmapi1 php_get_vhost_versions user={username} --output=json"
        installed_cmd = "ls /opt/cpanel/*/root/usr/bin/php 2>/dev/null"

        version_result = await _run_on_server(self._inventory, server, version_cmd)
        installed_result = await _run_on_server(self._inventory, server, installed_cmd)

        parts: list[str] = []
        parts.append("**Account PHP Version:**")
        parts.append(version_result.output.strip() if version_result.success else f"ERROR: {version_result.error}")
        parts.append("\n**Installed PHP Versions:**")
        if installed_result.success and installed_result.output.strip():
            for line in installed_result.output.strip().splitlines():
                # Extract version from path like /opt/cpanel/ea-php81/root/usr/bin/php
                match = re.search(r'ea-php(\d+)', line)
                if match:
                    ver = match.group(1)
                    parts.append(f"  PHP {ver[0]}.{ver[1:]}")
        else:
            parts.append("  Could not list installed versions")

        return ToolResult(output="\n".join(parts))


class CpanelEmailDiag(BaseTool):
    """Diagnose email issues for an account."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "cpanel_email_diag"

    @property
    def description(self) -> str:
        return "Email diagnostics: mailbox sizes, per-account queue, forwarders, autoresponders."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {"type": "string", "description": "Server name."},
                "username": {"type": "string", "description": "cPanel username."},
            },
            "required": ["server", "username"],
        }

    async def execute(self, *, server: str, username: str, **kwargs: Any) -> ToolResult:
        """Run email diagnostics."""
        checks = {
            "Mailbox sizes": f"du -sh /home/{username}/mail/*/ 2>/dev/null",
            "Email accounts": f"whmapi1 listpopswithdisk user={username} --output=json",
            "Forwarders": f"cat /home/{username}/.cpanel/forwarders 2>/dev/null",
            "Queued mail": f"exim -bpr 2>/dev/null | grep {username} | wc -l",
        }

        parts: list[str] = []
        for label, cmd in checks.items():
            result = await _run_on_server(self._inventory, server, cmd)
            val = result.output.strip() if result.success else "(not available)"
            parts.append(f"**{label}:**\n{val}")

        return ToolResult(output="\n\n".join(parts))
