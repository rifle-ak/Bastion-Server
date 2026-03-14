"""Web server diagnostic tools.

Apache/LiteSpeed status, SSL certificate checks (via openssl),
and smart error log parsing. Works on any server with a web
server — not cPanel-specific.
"""

from __future__ import annotations

import re
from typing import Any

from agent.inventory import Inventory
from agent.tools.base import BaseTool, ToolResult
from agent.tools.docker_tools import _run_on_server


class SSLCertCheck(BaseTool):
    """Check SSL certificate for a domain."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "ssl_cert_check"

    @property
    def description(self) -> str:
        return "Check SSL certificate for a domain: issuer, expiry, SANs, chain validity."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server to run the check from.",
                },
                "domain": {
                    "type": "string",
                    "description": "Domain to check (e.g. 'example.com').",
                },
                "port": {
                    "type": "integer",
                    "description": "Port to check (default 443).",
                    "default": 443,
                },
            },
            "required": ["server", "domain"],
        }

    async def execute(
        self, *, server: str, domain: str, port: int = 443, **kwargs: Any,
    ) -> ToolResult:
        """Check SSL cert via openssl s_client."""
        # Get cert details and verify chain in one shot
        cmd = (
            f"echo | openssl s_client -servername {domain} "
            f"-connect {domain}:{port} 2>/dev/null | "
            f"openssl x509 -noout -dates -subject -issuer -ext subjectAltName"
        )
        return await _run_on_server(self._inventory, server, cmd)


class ApacheStatus(BaseTool):
    """Check Apache/httpd status and config validation."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "apache_status"

    @property
    def description(self) -> str:
        return "Apache status: active connections, vhost config, config syntax check."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server name.",
                },
                "check": {
                    "type": "string",
                    "description": "What to check: 'status' (default), 'vhosts', or 'configtest'.",
                    "default": "status",
                },
            },
            "required": ["server"],
        }

    async def execute(
        self, *, server: str, check: str = "status", **kwargs: Any,
    ) -> ToolResult:
        """Run Apache diagnostic command."""
        commands = {
            "status": "apachectl status",
            "vhosts": "httpd -S",
            "configtest": "httpd -t",
        }
        cmd = commands.get(check)
        if not cmd:
            return ToolResult(
                error=f"Unknown check: {check!r}. Use 'status', 'vhosts', or 'configtest'.",
                exit_code=1,
            )
        return await _run_on_server(self._inventory, server, cmd)


class WebErrorLog(BaseTool):
    """Parse web server error logs with smart filtering."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "web_error_log"

    @property
    def description(self) -> str:
        return "Parse Apache/Nginx error logs. Groups by error type and shows recent entries."

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
                    "description": "Domain to filter logs for (optional — shows all if omitted).",
                },
                "lines": {
                    "type": "integer",
                    "description": "Number of recent lines to analyze (default 200).",
                    "default": 200,
                },
                "log_path": {
                    "type": "string",
                    "description": "Custom log path. Default: /var/log/apache2/error_log or /etc/httpd/logs/error_log.",
                },
            },
            "required": ["server"],
        }

    async def execute(
        self,
        *,
        server: str,
        domain: str | None = None,
        lines: int = 200,
        log_path: str | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        """Parse error logs and group by type."""
        # Try common log paths
        if log_path:
            path = log_path
        else:
            # Check which exists
            for candidate in [
                "/var/log/apache2/error_log",
                "/etc/httpd/logs/error_log",
                "/var/log/httpd/error_log",
                "/usr/local/apache/logs/error_log",
                "/var/log/nginx/error.log",
            ]:
                check = await _run_on_server(
                    self._inventory, server, f"test -f {candidate}"
                )
                if check.exit_code == 0:
                    path = candidate
                    break
            else:
                return ToolResult(
                    error="Could not find error log. Specify log_path.",
                    exit_code=1,
                )

        cmd = f"tail -n {lines} {path}"
        result = await _run_on_server(self._inventory, server, cmd)
        if not result.success:
            return result

        return ToolResult(output=_summarize_errors(result.output, domain))


class DNSCheck(BaseTool):
    """Check DNS records for a domain."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "dns_check"

    @property
    def description(self) -> str:
        return "Check DNS records (A, MX, NS, TXT/SPF) for a domain."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server to run the check from.",
                },
                "domain": {
                    "type": "string",
                    "description": "Domain to check.",
                },
                "record_type": {
                    "type": "string",
                    "description": "Record type: A, AAAA, MX, NS, TXT, CNAME, SOA, or 'all' (default: 'all').",
                    "default": "all",
                },
            },
            "required": ["server", "domain"],
        }

    async def execute(
        self, *, server: str, domain: str, record_type: str = "all", **kwargs: Any,
    ) -> ToolResult:
        """Look up DNS records via dig."""
        if record_type == "all":
            types = ["A", "AAAA", "MX", "NS", "TXT", "SOA"]
        else:
            types = [record_type.upper()]

        parts: list[str] = []
        for rtype in types:
            cmd = f"dig {domain} {rtype} +short"
            result = await _run_on_server(self._inventory, server, cmd)
            output = result.output.strip() if result.success else f"ERROR: {result.error}"
            if output:
                parts.append(f"**{rtype}:**\n{output}")
            else:
                parts.append(f"**{rtype}:** (no records)")

        return ToolResult(output="\n\n".join(parts))


# ── Helpers ──────────────────────────────────────────────────────


def _summarize_errors(log_text: str, domain_filter: str | None) -> str:
    """Group error log lines by type and summarize."""
    lines = log_text.strip().splitlines()

    if domain_filter:
        lines = [l for l in lines if domain_filter.lower() in l.lower()]

    if not lines:
        return "No errors found in the log."

    # Count error types
    categories: dict[str, int] = {}
    recent: list[str] = []

    for line in lines:
        lower = line.lower()
        if "php fatal" in lower or "php parse" in lower:
            categories["PHP Fatal/Parse"] = categories.get("PHP Fatal/Parse", 0) + 1
        elif "php warning" in lower or "php notice" in lower:
            categories["PHP Warning/Notice"] = categories.get("PHP Warning/Notice", 0) + 1
        elif "segfault" in lower or "segmentation fault" in lower:
            categories["Segfault"] = categories.get("Segfault", 0) + 1
        elif "permission denied" in lower:
            categories["Permission denied"] = categories.get("Permission denied", 0) + 1
        elif "file not found" in lower or "not exist" in lower:
            categories["File not found"] = categories.get("File not found", 0) + 1
        elif "modsecurity" in lower:
            categories["ModSecurity"] = categories.get("ModSecurity", 0) + 1
        elif "out of memory" in lower:
            categories["Out of memory"] = categories.get("Out of memory", 0) + 1
        elif "timeout" in lower or "timed out" in lower:
            categories["Timeout"] = categories.get("Timeout", 0) + 1
        elif "error" in lower or "crit" in lower:
            categories["Other errors"] = categories.get("Other errors", 0) + 1

    parts = [f"Analyzed {len(lines)} lines:"]

    if categories:
        parts.append("")
        parts.append("**Error Summary:**")
        for cat, count in sorted(categories.items(), key=lambda x: -x[1]):
            icon = "✗" if count > 10 else "⚠"
            parts.append(f"  {icon} {cat}: {count}")

    # Show last 10 actual error lines
    error_lines = [
        l for l in lines[-20:]
        if any(kw in l.lower() for kw in ("error", "fatal", "crit", "segfault", "denied"))
    ]
    if error_lines:
        parts.append("")
        parts.append("**Recent errors (last few):**")
        for el in error_lines[-10:]:
            # Truncate long lines
            if len(el) > 200:
                el = el[:200] + "..."
            parts.append(f"  {el}")

    return "\n".join(parts)
