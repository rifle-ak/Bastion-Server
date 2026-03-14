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


class AccessLogAnalysis(BaseTool):
    """Analyze web access logs for abuse, top IPs, and status codes."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "access_log_analysis"

    @property
    def description(self) -> str:
        return (
            "Analyze access logs: top IPs by request count, HTTP status "
            "breakdown, bandwidth hogs, potential abuse (scanners, bots)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {"type": "string", "description": "Server name."},
                "lines": {
                    "type": "integer",
                    "description": "Lines to analyze from end of log (default 5000).",
                    "default": 5000,
                },
                "domain": {
                    "type": "string",
                    "description": "Filter by domain (optional).",
                },
                "log_path": {
                    "type": "string",
                    "description": "Custom log path (auto-detected if omitted).",
                },
            },
            "required": ["server"],
        }

    async def execute(
        self,
        *,
        server: str,
        lines: int = 5000,
        domain: str | None = None,
        log_path: str | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        """Analyze access logs."""
        if log_path:
            path = log_path
        else:
            # Try cPanel domlogs first, then standard paths
            candidates = []
            if domain:
                candidates.append(f"/var/log/apache2/domlogs/{domain}")
                candidates.append(f"/etc/httpd/domlogs/{domain}")
            candidates.extend([
                "/var/log/apache2/access_log",
                "/etc/httpd/logs/access_log",
                "/var/log/httpd/access_log",
                "/usr/local/apache/logs/access_log",
                "/var/log/nginx/access.log",
            ])
            for candidate in candidates:
                check = await _run_on_server(self._inventory, server, f"test -f {candidate}")
                if check.exit_code == 0:
                    path = candidate
                    break
            else:
                return ToolResult(error="Access log not found. Specify log_path.", exit_code=1)

        # Get top IPs, status codes, and request counts in one pass
        # using awk for efficiency on large logs
        cmd = f"tail -n {lines} {path}"
        result = await _run_on_server(self._inventory, server, cmd)
        if not result.success:
            return result

        return ToolResult(output=_analyze_access_log(result.output))


class ModSecurityLog(BaseTool):
    """Parse ModSecurity blocks and extract rule details."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "modsecurity_log"

    @property
    def description(self) -> str:
        return (
            "Parse ModSecurity blocks: rule IDs that fired, blocked URIs, "
            "source IPs, and frequency. Helps identify false positives."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {"type": "string", "description": "Server name."},
                "lines": {
                    "type": "integer",
                    "description": "Lines to analyze (default 500).",
                    "default": 500,
                },
                "domain": {
                    "type": "string",
                    "description": "Filter by domain (optional).",
                },
            },
            "required": ["server"],
        }

    async def execute(
        self, *, server: str, lines: int = 500, domain: str | None = None, **kwargs: Any,
    ) -> ToolResult:
        """Parse ModSecurity entries from error log."""
        # Try common error log paths
        for candidate in [
            "/var/log/apache2/error_log",
            "/etc/httpd/logs/error_log",
            "/var/log/httpd/error_log",
            "/usr/local/apache/logs/error_log",
        ]:
            check = await _run_on_server(self._inventory, server, f"test -f {candidate}")
            if check.exit_code == 0:
                path = candidate
                break
        else:
            return ToolResult(error="Error log not found.", exit_code=1)

        cmd = f"tail -n {lines} {path}"
        result = await _run_on_server(self._inventory, server, cmd)
        if not result.success:
            return result

        return ToolResult(output=_analyze_modsec(result.output, domain))


# ── Helpers ──────────────────────────────────────────────────────


def _analyze_access_log(log_text: str) -> str:
    """Analyze access log and produce forensics report."""
    lines = log_text.strip().splitlines()
    if not lines:
        return "No log entries to analyze."

    ip_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    uri_counts: dict[str, int] = {}
    total = 0

    # Combined Log Format:
    # IP ident user [date] "METHOD URI PROTO" STATUS SIZE "referer" "UA"
    # The quoted request makes naive split() unreliable for field positions.
    # Use regex to extract key fields reliably.
    log_pattern = re.compile(
        r'^(\S+)'                    # IP
        r'\s+\S+\s+\S+'             # ident, user
        r'\s+\[[^\]]+\]'            # [date]
        r'\s+"(?:\S+)\s+(\S+)'      # "METHOD URI
        r'[^"]*"'                   # rest of request line"
        r'\s+(\d{3})'              # STATUS
    )

    for line in lines:
        total += 1
        m = log_pattern.match(line)
        if not m:
            continue

        ip, uri, status = m.group(1), m.group(2), m.group(3)
        ip_counts[ip] = ip_counts.get(ip, 0) + 1
        status_counts[status] = status_counts.get(status, 0) + 1
        uri_counts[uri] = uri_counts.get(uri, 0) + 1

    report: list[str] = [f"Analyzed {total} requests:\n"]

    # Top IPs
    top_ips = sorted(ip_counts.items(), key=lambda x: -x[1])[:15]
    report.append("**Top IPs by request count:**")
    for ip, count in top_ips:
        pct = (count / total) * 100
        flag = " ⚠" if count > total * 0.2 else ""
        report.append(f"  {count:>6} ({pct:4.1f}%) {ip}{flag}")

    # Status code breakdown
    report.append("\n**HTTP Status Codes:**")
    for code in sorted(status_counts.keys()):
        count = status_counts[code]
        pct = (count / total) * 100
        icon = ""
        if code.startswith("4"):
            icon = " ⚠" if count > 100 else ""
        elif code.startswith("5"):
            icon = " ✗"
        report.append(f"  {code}: {count:>6} ({pct:4.1f}%){icon}")

    # Most requested URIs (potential scanning)
    top_uris = sorted(uri_counts.items(), key=lambda x: -x[1])[:10]
    suspicious_uris = [
        u for u, c in top_uris
        if any(kw in u.lower() for kw in (
            "wp-login", "xmlrpc", ".env", "wp-admin/admin-ajax",
            "phpmyadmin", "/.git", "/config", "eval-stdin",
            "wp-cron", "wp-json",
        ))
    ]
    if suspicious_uris:
        report.append("\n**Frequently targeted URIs:**")
        for uri in suspicious_uris:
            report.append(f"  {uri_counts[uri]:>6} {uri}")

    # Detect potential brute force (high wp-login/xmlrpc hits from single IP)
    login_ips: dict[str, int] = {}
    for line in lines:
        if "wp-login" in line or "xmlrpc" in line:
            parts = line.split()
            ip = parts[0] if parts else ""
            if ip and re.match(r'\d+\.\d+\.\d+\.\d+', ip):
                login_ips[ip] = login_ips.get(ip, 0) + 1
    brute_force = [(ip, c) for ip, c in login_ips.items() if c > 20]
    if brute_force:
        report.append("\n**⚠ Potential brute force attempts:**")
        for ip, count in sorted(brute_force, key=lambda x: -x[1])[:5]:
            report.append(f"  {ip}: {count} login/xmlrpc requests")

    return "\n".join(report)


def _analyze_modsec(log_text: str, domain_filter: str | None) -> str:
    """Parse ModSecurity entries and produce a debugging report."""
    lines = log_text.strip().splitlines()

    # Filter for ModSecurity entries
    modsec_lines = [l for l in lines if "modsecurity" in l.lower() or "ModSecurity" in l]

    if domain_filter:
        modsec_lines = [l for l in modsec_lines if domain_filter.lower() in l.lower()]

    if not modsec_lines:
        return "No ModSecurity blocks found in the analyzed log range."

    # Extract rule IDs
    rule_counts: dict[str, int] = {}
    uri_counts: dict[str, int] = {}
    ip_counts: dict[str, int] = {}

    for line in modsec_lines:
        # Extract rule ID: [id "12345"] or [id 12345]
        id_match = re.search(r'\[id\s*"?(\d+)"?\]', line)
        if id_match:
            rule_id = id_match.group(1)
            rule_counts[rule_id] = rule_counts.get(rule_id, 0) + 1

        # Extract URI
        uri_match = re.search(r'\[uri\s*"([^"]+)"\]', line)
        if uri_match:
            uri = uri_match.group(1)
            uri_counts[uri] = uri_counts.get(uri, 0) + 1

        # Extract client IP
        ip_match = re.search(r'\[client\s+([\d.]+)', line)
        if ip_match:
            ip = ip_match.group(1)
            ip_counts[ip] = ip_counts.get(ip, 0) + 1

    report: list[str] = [f"**ModSecurity Blocks: {len(modsec_lines)} entries**\n"]

    # Top rule IDs
    if rule_counts:
        report.append("**Rule IDs (most frequent):**")
        for rule_id, count in sorted(rule_counts.items(), key=lambda x: -x[1])[:10]:
            report.append(f"  Rule {rule_id}: {count} blocks")

    # Blocked URIs
    if uri_counts:
        report.append("\n**Blocked URIs:**")
        for uri, count in sorted(uri_counts.items(), key=lambda x: -x[1])[:10]:
            report.append(f"  {count:>4}x {uri}")

    # Source IPs
    if ip_counts:
        report.append("\n**Source IPs:**")
        for ip, count in sorted(ip_counts.items(), key=lambda x: -x[1])[:10]:
            report.append(f"  {count:>4}x {ip}")

    # Last 5 entries for context
    report.append("\n**Last 5 blocks:**")
    for line in modsec_lines[-5:]:
        if len(line) > 200:
            line = line[:200] + "..."
        report.append(f"  {line}")

    return "\n".join(report)


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
