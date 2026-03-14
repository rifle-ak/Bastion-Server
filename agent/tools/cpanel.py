"""cPanel/WHM management tools.

Wraps WHM API (whmapi1) calls for account management, SSL status,
backup reporting, and email deliverability checks. All commands
are built programmatically and run via the existing SSH pipeline.
"""

from __future__ import annotations

import json
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
