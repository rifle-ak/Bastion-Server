"""Configuration diff and baseline checking tools.

Compares config files between servers to detect drift, and checks
individual server configs against known-good security baselines.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from agent.inventory import Inventory
from agent.tools.base import BaseTool, ToolResult
from agent.tools.docker_tools import _run_on_server


# ---------------------------------------------------------------------------
# Config path definitions by server type
# ---------------------------------------------------------------------------

CPANEL_PATHS: list[str] = [
    "/etc/apache2/conf/httpd.conf",
    "/etc/httpd/conf/httpd.conf",
    "/usr/local/cpanel/cpanel.config",
    "/var/cpanel/cpanel.config",
    "/etc/my.cnf",
    "/usr/local/lib/php.ini",
    "/etc/csf/csf.conf",
]

PTERODACTYL_PATHS: list[str] = [
    "/etc/pterodactyl/config.yml",
    "/etc/docker/daemon.json",
]

GENERAL_PATHS: list[str] = [
    "/etc/ssh/sshd_config",
    "/etc/sysctl.conf",
]


def _paths_for_type(config_type: str) -> list[str]:
    """Return the config file paths for a given server type."""
    mapping: dict[str, list[str]] = {
        "cpanel": CPANEL_PATHS + GENERAL_PATHS,
        "pterodactyl": PTERODACTYL_PATHS + GENERAL_PATHS,
        "general": GENERAL_PATHS,
    }
    return mapping.get(config_type, GENERAL_PATHS)


# ---------------------------------------------------------------------------
# Diff helpers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DiffChange:
    """A single difference between two config texts."""

    line_number: int
    change_type: str  # "added", "removed", "modified"
    content_a: str
    content_b: str


def _simple_diff(text_a: str, text_b: str) -> list[DiffChange]:
    """Compare two config texts line by line and return meaningful diffs.

    Blank lines and comment-only differences are included so that
    security-relevant changes in comments are not silently hidden.
    """
    lines_a = text_a.splitlines()
    lines_b = text_b.splitlines()
    changes: list[DiffChange] = []
    max_lines = max(len(lines_a), len(lines_b))

    for i in range(max_lines):
        a_line = lines_a[i] if i < len(lines_a) else ""
        b_line = lines_b[i] if i < len(lines_b) else ""
        if a_line == b_line:
            continue
        if i >= len(lines_a):
            changes.append(DiffChange(i + 1, "added", "", b_line))
        elif i >= len(lines_b):
            changes.append(DiffChange(i + 1, "removed", a_line, ""))
        else:
            changes.append(DiffChange(i + 1, "modified", a_line, b_line))

    return changes


# ---------------------------------------------------------------------------
# Security-relevant pattern detection
# ---------------------------------------------------------------------------

SECURITY_KEYWORDS: list[str] = [
    "PermitRootLogin",
    "PasswordAuthentication",
    "Port ",
    "bind-address",
    "skip-grant-tables",
    "expose_php",
    "display_errors",
    "disable_functions",
    "ServerTokens",
    "ServerSignature",
    "AllowOverride",
    "live-restore",
    "userns-remap",
    "max_connections",
    "RESTRICT_SYSLOG",
    "TCP_IN",
    "TCP_OUT",
]


def _is_security_relevant(line: str) -> bool:
    """Check if a config line touches a security-relevant setting."""
    stripped = line.strip()
    return any(kw in stripped for kw in SECURITY_KEYWORDS)


# ---------------------------------------------------------------------------
# Report builders (standalone for testability)
# ---------------------------------------------------------------------------

def _build_diff_report(
    results_a: dict[str, str],
    results_b: dict[str, str],
    server_a: str,
    server_b: str,
) -> str:
    """Build a human-readable diff report from fetched configs.

    Args:
        results_a: Mapping of file path to file contents for server A.
        results_b: Mapping of file path to file contents for server B.
        server_a: Name of the first server.
        server_b: Name of the second server.

    Returns:
        Formatted diff report string.
    """
    all_paths = sorted(set(results_a.keys()) | set(results_b.keys()))
    sections: list[str] = []
    total_diffs = 0

    for path in all_paths:
        text_a = results_a.get(path)
        text_b = results_b.get(path)

        if text_a is None and text_b is None:
            continue
        if text_a is None:
            sections.append(f"### {path}\nOnly present on {server_b} (missing from {server_a})")
            total_diffs += 1
            continue
        if text_b is None:
            sections.append(f"### {path}\nOnly present on {server_a} (missing from {server_b})")
            total_diffs += 1
            continue

        changes = _simple_diff(text_a, text_b)
        if not changes:
            sections.append(f"### {path}\nIdentical on both servers.")
            continue

        total_diffs += len(changes)
        lines: list[str] = [f"### {path}", f"{len(changes)} difference(s) found:"]
        for ch in changes:
            marker = ""
            if _is_security_relevant(ch.content_a) or _is_security_relevant(ch.content_b):
                marker = " [SECURITY]"
            if ch.change_type == "added":
                lines.append(f"  L{ch.line_number} +{server_b}: {ch.content_b}{marker}")
            elif ch.change_type == "removed":
                lines.append(f"  L{ch.line_number} -{server_a}: {ch.content_a}{marker}")
            else:
                lines.append(f"  L{ch.line_number}{marker}")
                lines.append(f"    {server_a}: {ch.content_a}")
                lines.append(f"    {server_b}: {ch.content_b}")
        sections.append("\n".join(lines))

    header = (
        f"# Config Diff: {server_a} vs {server_b}\n"
        f"Total differences: {total_diffs}\n"
    )
    return header + "\n\n" + "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Baseline checking
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BaselineCheck:
    """Result of a single baseline check."""

    category: str
    setting: str
    status: str  # "PASS", "WARN", "FAIL"
    explanation: str


def _check_ssh_baseline(content: str) -> list[BaselineCheck]:
    """Check sshd_config against security baselines."""
    checks: list[BaselineCheck] = []
    lines = {l.strip().split()[0]: l.strip() for l in content.splitlines()
             if l.strip() and not l.strip().startswith("#")}

    # PermitRootLogin
    val = lines.get("PermitRootLogin", "")
    if "no" in val.lower():
        checks.append(BaselineCheck("SSH", "PermitRootLogin", "PASS", "Root login is disabled."))
    elif "prohibit-password" in val.lower() or "without-password" in val.lower():
        checks.append(BaselineCheck("SSH", "PermitRootLogin", "WARN",
                                    "Root login allowed with key only. Prefer disabling entirely."))
    else:
        checks.append(BaselineCheck("SSH", "PermitRootLogin", "FAIL",
                                    f"Root login may be enabled: {val or 'not explicitly set (default: yes)'}"))

    # PasswordAuthentication
    val = lines.get("PasswordAuthentication", "")
    if "no" in val.lower():
        checks.append(BaselineCheck("SSH", "PasswordAuthentication", "PASS",
                                    "Password authentication is disabled."))
    else:
        checks.append(BaselineCheck("SSH", "PasswordAuthentication", "WARN",
                                    "Password authentication is not explicitly disabled."))

    # Port
    val = lines.get("Port", "")
    if val and "22" in val.split()[-1:]:
        checks.append(BaselineCheck("SSH", "Port", "WARN",
                                    "SSH is running on default port 22."))
    elif val:
        checks.append(BaselineCheck("SSH", "Port", "PASS",
                                    f"SSH on non-default port: {val.split()[-1]}"))
    else:
        checks.append(BaselineCheck("SSH", "Port", "WARN",
                                    "SSH port not explicitly set (defaults to 22)."))

    return checks


def _check_mysql_baseline(content: str) -> list[BaselineCheck]:
    """Check MySQL/MariaDB config against baselines."""
    checks: list[BaselineCheck] = []
    lower = content.lower()

    # bind-address
    if "bind-address" in lower:
        for line in content.splitlines():
            stripped = line.strip().lower()
            if stripped.startswith("bind-address") or stripped.startswith("bind_address"):
                if "0.0.0.0" in stripped or "::" in stripped:
                    checks.append(BaselineCheck("MySQL", "bind-address", "FAIL",
                                                "MySQL is bound to all interfaces. Restrict to 127.0.0.1."))
                elif "127.0.0.1" in stripped or "localhost" in stripped:
                    checks.append(BaselineCheck("MySQL", "bind-address", "PASS",
                                                "MySQL is bound to localhost only."))
                else:
                    checks.append(BaselineCheck("MySQL", "bind-address", "WARN",
                                                f"MySQL bind-address: {stripped}"))
                break
    else:
        checks.append(BaselineCheck("MySQL", "bind-address", "WARN",
                                    "bind-address not explicitly set."))

    # skip-grant-tables
    if "skip-grant-tables" in lower:
        checks.append(BaselineCheck("MySQL", "skip-grant-tables", "FAIL",
                                    "skip-grant-tables is present. Authentication is bypassed!"))
    else:
        checks.append(BaselineCheck("MySQL", "skip-grant-tables", "PASS",
                                    "skip-grant-tables is not set."))

    # max_connections
    for line in content.splitlines():
        stripped = line.strip().lower()
        if stripped.startswith("max_connections"):
            parts = stripped.split("=")
            if len(parts) == 2:
                try:
                    val = int(parts[1].strip())
                    if val > 500:
                        checks.append(BaselineCheck("MySQL", "max_connections", "WARN",
                                                    f"max_connections={val} is high. Verify resource limits."))
                    else:
                        checks.append(BaselineCheck("MySQL", "max_connections", "PASS",
                                                    f"max_connections={val}."))
                except ValueError:
                    pass
            break

    return checks


def _check_php_baseline(content: str) -> list[BaselineCheck]:
    """Check PHP config against baselines."""
    checks: list[BaselineCheck] = []

    def _get_ini_value(key: str) -> str | None:
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith(key) and "=" in stripped:
                return stripped.split("=", 1)[1].strip()
        return None

    # expose_php
    val = _get_ini_value("expose_php")
    if val and val.lower() in ("off", "0", "false"):
        checks.append(BaselineCheck("PHP", "expose_php", "PASS", "PHP version not exposed in headers."))
    else:
        checks.append(BaselineCheck("PHP", "expose_php", "WARN",
                                    f"expose_php is {val or 'not set (default: On)'}. Should be Off in production."))

    # display_errors
    val = _get_ini_value("display_errors")
    if val and val.lower() in ("off", "0", "false"):
        checks.append(BaselineCheck("PHP", "display_errors", "PASS", "Error display is off."))
    else:
        checks.append(BaselineCheck("PHP", "display_errors", "WARN",
                                    f"display_errors is {val or 'not set'}. Should be Off in production."))

    # memory_limit
    val = _get_ini_value("memory_limit")
    if val:
        checks.append(BaselineCheck("PHP", "memory_limit", "PASS", f"memory_limit={val}."))
    else:
        checks.append(BaselineCheck("PHP", "memory_limit", "WARN", "memory_limit not explicitly set."))

    # upload_max_filesize
    val = _get_ini_value("upload_max_filesize")
    if val:
        checks.append(BaselineCheck("PHP", "upload_max_filesize", "PASS",
                                    f"upload_max_filesize={val}."))

    # disable_functions
    val = _get_ini_value("disable_functions")
    if val and len(val) > 5:
        checks.append(BaselineCheck("PHP", "disable_functions", "PASS",
                                    "Dangerous functions are disabled."))
    else:
        checks.append(BaselineCheck("PHP", "disable_functions", "WARN",
                                    "disable_functions is empty or minimal. Consider disabling exec, system, etc."))

    return checks


def _check_apache_baseline(content: str) -> list[BaselineCheck]:
    """Check Apache/httpd config against baselines."""
    checks: list[BaselineCheck] = []
    lower = content.lower()

    # ServerTokens
    if "servertokens" in lower:
        for line in content.splitlines():
            if line.strip().lower().startswith("servertokens"):
                val = line.strip().split()[-1].lower()
                if val in ("prod", "productonly"):
                    checks.append(BaselineCheck("Apache", "ServerTokens", "PASS",
                                                "ServerTokens set to Prod."))
                else:
                    checks.append(BaselineCheck("Apache", "ServerTokens", "WARN",
                                                f"ServerTokens={val}. Should be Prod to minimize info disclosure."))
                break
    else:
        checks.append(BaselineCheck("Apache", "ServerTokens", "WARN",
                                    "ServerTokens not set. Defaults to Full (information disclosure)."))

    # ServerSignature
    if "serversignature" in lower:
        for line in content.splitlines():
            if line.strip().lower().startswith("serversignature"):
                val = line.strip().split()[-1].lower()
                if val == "off":
                    checks.append(BaselineCheck("Apache", "ServerSignature", "PASS",
                                                "ServerSignature is off."))
                else:
                    checks.append(BaselineCheck("Apache", "ServerSignature", "WARN",
                                                f"ServerSignature={val}. Should be Off."))
                break
    else:
        checks.append(BaselineCheck("Apache", "ServerSignature", "WARN",
                                    "ServerSignature not set. Defaults to On."))

    # Directory listing (Options Indexes)
    if "options" in lower and "indexes" in lower:
        checks.append(BaselineCheck("Apache", "DirectoryListing", "WARN",
                                    "Options Indexes found. Directory listing may be enabled."))
    else:
        checks.append(BaselineCheck("Apache", "DirectoryListing", "PASS",
                                    "No Options Indexes directive found."))

    return checks


def _check_docker_baseline(content: str) -> list[BaselineCheck]:
    """Check Docker daemon.json against baselines."""
    checks: list[BaselineCheck] = []
    lower = content.lower()

    # live-restore
    if '"live-restore"' in lower:
        if '"live-restore": true' in lower or '"live-restore":true' in lower:
            checks.append(BaselineCheck("Docker", "live-restore", "PASS",
                                        "live-restore is enabled."))
        else:
            checks.append(BaselineCheck("Docker", "live-restore", "WARN",
                                        "live-restore is present but not true."))
    else:
        checks.append(BaselineCheck("Docker", "live-restore", "WARN",
                                    "live-restore not configured. Containers will stop on daemon restart."))

    # Log rotation
    if '"log-driver"' in lower or '"max-size"' in lower:
        checks.append(BaselineCheck("Docker", "log-rotation", "PASS",
                                    "Log driver or max-size is configured."))
    else:
        checks.append(BaselineCheck("Docker", "log-rotation", "WARN",
                                    "No log rotation configured. Logs may grow unbounded."))

    # userns-remap
    if '"userns-remap"' in lower:
        checks.append(BaselineCheck("Docker", "userns-remap", "PASS",
                                    "User namespace remapping is configured."))
    else:
        checks.append(BaselineCheck("Docker", "userns-remap", "WARN",
                                    "userns-remap not set. Containers run as host root."))

    return checks


def _check_pterodactyl_baseline(content: str) -> list[BaselineCheck]:
    """Check Pterodactyl Wings config.yml against baselines."""
    checks: list[BaselineCheck] = []
    lower = content.lower()

    # SFTP
    if "sftp:" in lower:
        checks.append(BaselineCheck("Pterodactyl", "SFTP", "PASS",
                                    "SFTP configuration section is present."))
    else:
        checks.append(BaselineCheck("Pterodactyl", "SFTP", "WARN",
                                    "No SFTP configuration found in Wings config."))

    # allowed_mounts
    if "allowed_mounts" in lower:
        checks.append(BaselineCheck("Pterodactyl", "allowed_mounts", "WARN",
                                    "allowed_mounts is configured. Verify paths are restricted."))
    else:
        checks.append(BaselineCheck("Pterodactyl", "allowed_mounts", "PASS",
                                    "No allowed_mounts configured (restrictive default)."))

    # throttles
    if "throttles:" in lower:
        checks.append(BaselineCheck("Pterodactyl", "throttles", "PASS",
                                    "Throttle configuration is present."))
    else:
        checks.append(BaselineCheck("Pterodactyl", "throttles", "WARN",
                                    "No throttle configuration found."))

    return checks


def _check_firewall_active(content: str) -> list[BaselineCheck]:
    """Check if firewall output indicates an active firewall."""
    checks: list[BaselineCheck] = []
    lower = content.lower()

    if "status: active" in lower or "chain input" in lower or "table" in lower:
        checks.append(BaselineCheck("Firewall", "status", "PASS", "Firewall appears active."))
    elif "error" in lower or "not found" in lower or not content.strip():
        checks.append(BaselineCheck("Firewall", "status", "FAIL",
                                    "Could not confirm firewall is active."))
    else:
        checks.append(BaselineCheck("Firewall", "status", "WARN",
                                    f"Firewall status unclear: {content[:120]}"))

    return checks


def _build_baseline_report(
    checks: list[BaselineCheck],
    server: str,
) -> str:
    """Build a human-readable baseline report from check results.

    Args:
        checks: List of completed baseline checks.
        server: Server name for the report header.

    Returns:
        Formatted baseline report string.
    """
    passes = sum(1 for c in checks if c.status == "PASS")
    warns = sum(1 for c in checks if c.status == "WARN")
    fails = sum(1 for c in checks if c.status == "FAIL")

    lines: list[str] = [
        f"# Baseline Report: {server}",
        f"PASS: {passes} | WARN: {warns} | FAIL: {fails}",
        "",
    ]

    # Group by category
    categories: dict[str, list[BaselineCheck]] = {}
    for check in checks:
        categories.setdefault(check.category, []).append(check)

    for category, cat_checks in categories.items():
        lines.append(f"## {category}")
        for ch in cat_checks:
            icon = {"PASS": "[PASS]", "WARN": "[WARN]", "FAIL": "[FAIL]"}[ch.status]
            lines.append(f"  {icon} {ch.setting}: {ch.explanation}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

async def _fetch_file(
    inventory: Inventory,
    server: str,
    path: str,
) -> tuple[str, str | None]:
    """Fetch a config file from a server, returning (path, content_or_None)."""
    result = await _run_on_server(inventory, server, f"cat {path}")
    if result.exit_code != 0 or not result.output.strip():
        return (path, None)
    return (path, result.output)


async def _fetch_files(
    inventory: Inventory,
    server: str,
    paths: list[str],
) -> dict[str, str]:
    """Fetch multiple config files in parallel, skipping missing ones."""
    tasks = [_fetch_file(inventory, server, p) for p in paths]
    results = await asyncio.gather(*tasks)
    return {path: content for path, content in results if content is not None}


async def _detect_config_type(
    inventory: Inventory,
    server: str,
) -> str:
    """Auto-detect the server type by probing for known config files."""
    cpanel_result = await _run_on_server(
        inventory, server, "test -f /usr/local/cpanel/cpanel.config && echo cpanel"
    )
    if cpanel_result.exit_code == 0 and "cpanel" in cpanel_result.output.lower():
        return "cpanel"

    ptero_result = await _run_on_server(
        inventory, server, "test -f /etc/pterodactyl/config.yml && echo pterodactyl"
    )
    if ptero_result.exit_code == 0 and "pterodactyl" in ptero_result.output.lower():
        return "pterodactyl"

    return "general"


# ---------------------------------------------------------------------------
# Tool: ConfigDiff
# ---------------------------------------------------------------------------

class ConfigDiff(BaseTool):
    """Compare configuration files between two servers to detect drift."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "config_diff"

    @property
    def description(self) -> str:
        return (
            "Compare critical config files between two servers to find configuration drift. "
            "Supports cPanel/WHM, Pterodactyl/Pelican, and general Linux configs. "
            "Highlights security-relevant differences."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server_a": {
                    "type": "string",
                    "description": "First server name from the inventory.",
                },
                "server_b": {
                    "type": "string",
                    "description": "Second server name from the inventory.",
                },
                "config_type": {
                    "type": "string",
                    "description": (
                        "Type of configs to compare: 'cpanel', 'pterodactyl', 'general'. "
                        "Defaults to auto-detect."
                    ),
                    "enum": ["cpanel", "pterodactyl", "general"],
                },
                "custom_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Additional config file paths to compare.",
                },
            },
            "required": ["server_a", "server_b"],
        }

    async def execute(
        self,
        *,
        server_a: str,
        server_b: str,
        config_type: str | None = None,
        custom_paths: list[str] | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        """Fetch configs from both servers and produce a diff report."""
        # Validate servers exist
        for name in (server_a, server_b):
            try:
                self._inventory.get_server(name)
            except KeyError as e:
                return ToolResult(error=str(e), exit_code=1)

        # Determine config type
        if config_type is None:
            config_type = await _detect_config_type(self._inventory, server_a)

        paths = _paths_for_type(config_type)
        if custom_paths:
            paths = paths + [p for p in custom_paths if p not in paths]

        # Fetch from both servers in parallel
        results_a, results_b = await asyncio.gather(
            _fetch_files(self._inventory, server_a, paths),
            _fetch_files(self._inventory, server_b, paths),
        )

        # Only report on paths that exist on at least one server
        all_found = set(results_a.keys()) | set(results_b.keys())
        if not all_found:
            return ToolResult(
                output=f"No config files found on either {server_a} or {server_b} for type '{config_type}'.",
            )

        report = _build_diff_report(results_a, results_b, server_a, server_b)
        return ToolResult(output=report)


# ---------------------------------------------------------------------------
# Tool: ConfigBaseline
# ---------------------------------------------------------------------------

# Mapping of config type to (file path, checker function) pairs
_BASELINE_CHECKS: dict[str, list[tuple[str, Any]]] = {
    "general": [
        ("/etc/ssh/sshd_config", _check_ssh_baseline),
    ],
    "cpanel": [
        ("/etc/ssh/sshd_config", _check_ssh_baseline),
        ("/etc/my.cnf", _check_mysql_baseline),
        ("/usr/local/lib/php.ini", _check_php_baseline),
        ("/etc/apache2/conf/httpd.conf", _check_apache_baseline),
        ("/etc/httpd/conf/httpd.conf", _check_apache_baseline),
    ],
    "pterodactyl": [
        ("/etc/ssh/sshd_config", _check_ssh_baseline),
        ("/etc/docker/daemon.json", _check_docker_baseline),
        ("/etc/pterodactyl/config.yml", _check_pterodactyl_baseline),
    ],
}


class ConfigBaseline(BaseTool):
    """Check a server's configuration against known security baselines."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "config_baseline"

    @property
    def description(self) -> str:
        return (
            "Check a server's config files against known best-practice security baselines. "
            "Returns PASS/WARN/FAIL for SSH, MySQL, PHP, Apache, Docker, Pterodactyl, "
            "and firewall settings."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server name from the inventory.",
                },
                "config_type": {
                    "type": "string",
                    "description": (
                        "Type of configs to check: 'cpanel', 'pterodactyl', 'general'. "
                        "Defaults to auto-detect."
                    ),
                    "enum": ["cpanel", "pterodactyl", "general"],
                },
            },
            "required": ["server"],
        }

    async def execute(
        self,
        *,
        server: str,
        config_type: str | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        """Run baseline checks against a server's configuration."""
        try:
            self._inventory.get_server(server)
        except KeyError as e:
            return ToolResult(error=str(e), exit_code=1)

        # Determine config type
        if config_type is None:
            config_type = await _detect_config_type(self._inventory, server)

        check_defs = _BASELINE_CHECKS.get(config_type, _BASELINE_CHECKS["general"])

        # Fetch all config files in parallel
        paths = list({path for path, _ in check_defs})
        fetched = await _fetch_files(self._inventory, server, paths)

        # Run checkers
        all_checks: list[BaselineCheck] = []
        seen_paths: set[str] = set()
        for path, checker in check_defs:
            if path in seen_paths:
                continue
            content = fetched.get(path)
            if content is None:
                continue
            seen_paths.add(path)
            all_checks.extend(checker(content))

        # Firewall check (try multiple tools)
        fw_result = await _run_on_server(
            self._inventory, server, "ufw status 2>/dev/null || iptables-save 2>/dev/null || echo not found"
        )
        all_checks.extend(_check_firewall_active(fw_result.output))

        if not all_checks:
            return ToolResult(
                output=f"No config files found on {server} for type '{config_type}'. "
                       "Cannot perform baseline check.",
            )

        report = _build_baseline_report(all_checks, server)
        return ToolResult(output=report)
