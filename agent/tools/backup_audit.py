"""Backup health auditing tool.

Audits backup status across servers, supporting cPanel, Pterodactyl/Pelican,
and MySQL/MariaDB backup systems. Checks backup recency, sizes, storage
space, and optionally verifies archive integrity.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from agent.inventory import Inventory
from agent.tools.base import BaseTool, ToolResult
from agent.tools.docker_tools import _run_on_server

# Thresholds for backup staleness
_WARN_HOURS = 24
_CRITICAL_HOURS = 48

# Minimum plausible backup size in bytes (1 KB) — anything smaller is suspect
_MIN_BACKUP_SIZE_BYTES = 1024


def _build_backup_report(server: str, data: dict[str, Any]) -> str:
    """Build a human-readable backup health report from collected data.

    This is a standalone function (not a method) for testability.

    Args:
        server: Server name for the report header.
        data: Dict with keys for each backup subsystem containing parsed
              results from the audit commands.

    Returns:
        Formatted multi-section report string.
    """
    lines: list[str] = []
    lines.append(f"=== BACKUP AUDIT: {server} ===")
    lines.append(f"Audit time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append("")

    recommendations: list[str] = []

    # --- Storage space ---
    storage = data.get("storage")
    if storage:
        lines.append("--- STORAGE ---")
        lines.append(storage)
        lines.append("")
        # Parse df output for usage warnings
        for line in storage.splitlines():
            match = re.search(r"(\d+)%", line)
            if match:
                usage_pct = int(match.group(1))
                if usage_pct >= 90:
                    recommendations.append(
                        f"CRITICAL: Backup partition at {usage_pct}% capacity. "
                        "Free space immediately to avoid backup failures."
                    )
                elif usage_pct >= 80:
                    recommendations.append(
                        f"WARN: Backup partition at {usage_pct}% capacity. "
                        "Plan storage expansion."
                    )

    # --- cPanel backups ---
    cpanel = data.get("cpanel")
    if cpanel:
        lines.append("--- CPANEL BACKUPS ---")
        _format_backup_section(lines, recommendations, cpanel, "cpanel")

    # --- JetBackup ---
    jetbackup = data.get("jetbackup")
    if jetbackup:
        lines.append("--- JETBACKUP ---")
        if jetbackup.get("installed"):
            lines.append("JetBackup 5: installed")
            if jetbackup.get("details"):
                lines.append(jetbackup["details"])
        else:
            lines.append("JetBackup 5: not installed")
        lines.append("")

    # --- cPanel backup config ---
    cpanel_config = data.get("cpanel_config")
    if cpanel_config:
        lines.append("--- CPANEL BACKUP CONFIG ---")
        lines.append(cpanel_config)
        lines.append("")

    # --- Pterodactyl backups ---
    pterodactyl = data.get("pterodactyl")
    if pterodactyl:
        lines.append("--- PTERODACTYL/PELICAN BACKUPS ---")
        _format_backup_section(lines, recommendations, pterodactyl, "pterodactyl")

    # --- MySQL backups ---
    mysql = data.get("mysql")
    if mysql:
        lines.append("--- MYSQL/MARIADB BACKUPS ---")
        _format_backup_section(lines, recommendations, mysql, "mysql")

        if mysql.get("binlog"):
            lines.append(f"Binary logging: {mysql['binlog']}")
            lines.append("")

    # --- Integrity checks ---
    integrity = data.get("integrity")
    if integrity:
        lines.append("--- INTEGRITY CHECKS ---")
        for check in integrity:
            status = "OK" if check.get("valid") else "FAILED"
            lines.append(f"  [{status}] {check.get('file', 'unknown')}")
            if not check.get("valid") and check.get("error"):
                lines.append(f"         Error: {check['error']}")
        lines.append("")

    # --- Recommendations ---
    if recommendations:
        lines.append("--- RECOMMENDATIONS ---")
        for i, rec in enumerate(recommendations, 1):
            lines.append(f"  {i}. {rec}")
    else:
        lines.append("--- RECOMMENDATIONS ---")
        lines.append("  No issues detected. Backups appear healthy.")

    return "\n".join(lines)


def _format_backup_section(
    lines: list[str],
    recommendations: list[str],
    section_data: dict[str, Any],
    backup_type: str,
) -> None:
    """Format a backup subsystem section and append warnings.

    Args:
        lines: Report lines to append to.
        recommendations: Recommendations list to append warnings to.
        section_data: Parsed backup data for this subsystem.
        backup_type: Label for the backup type (used in warnings).
    """
    if section_data.get("error"):
        lines.append(f"  Error: {section_data['error']}")
        lines.append("")
        recommendations.append(
            f"CRITICAL: Could not inspect {backup_type} backups: {section_data['error']}"
        )
        return

    if section_data.get("not_found"):
        lines.append(f"  No {backup_type} backup directory found.")
        lines.append("")
        return

    file_list = section_data.get("files", [])
    if not file_list:
        lines.append("  No backup files found.")
        lines.append("")
        recommendations.append(
            f"CRITICAL: No {backup_type} backup files found. "
            "Verify backup schedule is configured and running."
        )
        return

    lines.append(f"  Found {len(file_list)} backup(s):")
    now = datetime.now(timezone.utc)
    newest_age_hours: float | None = None

    for entry in file_list:
        name = entry.get("name", "unknown")
        size = entry.get("size", "unknown")
        date_str = entry.get("date", "")
        lines.append(f"    {name}  size={size}  date={date_str}")

        # Check for suspiciously small backups
        size_bytes = _parse_size_bytes(size)
        if size_bytes is not None and size_bytes < _MIN_BACKUP_SIZE_BYTES:
            recommendations.append(
                f"WARN: {backup_type} backup '{name}' is suspiciously small "
                f"({size}). May be corrupted or empty."
            )

        # Track newest backup age
        parsed_date = _parse_backup_date(date_str)
        if parsed_date is not None:
            age_hours = (now - parsed_date).total_seconds() / 3600
            if newest_age_hours is None or age_hours < newest_age_hours:
                newest_age_hours = age_hours

    lines.append("")

    # Staleness check
    if newest_age_hours is not None:
        if newest_age_hours > _CRITICAL_HOURS:
            recommendations.append(
                f"CRITICAL: Most recent {backup_type} backup is "
                f"{newest_age_hours:.0f}h old (>{_CRITICAL_HOURS}h threshold)."
            )
        elif newest_age_hours > _WARN_HOURS:
            recommendations.append(
                f"WARN: Most recent {backup_type} backup is "
                f"{newest_age_hours:.0f}h old (>{_WARN_HOURS}h threshold)."
            )

    # Size trend (compare last 3 if available)
    if len(file_list) >= 3:
        sizes = []
        for entry in file_list[:3]:
            s = _parse_size_bytes(entry.get("size", ""))
            if s is not None:
                sizes.append(s)
        if len(sizes) >= 3 and sizes[0] > 0:
            ratio = sizes[0] / sizes[2] if sizes[2] > 0 else 0
            if ratio < 0.5:
                recommendations.append(
                    f"WARN: {backup_type} backup sizes are shrinking significantly. "
                    "Latest backup is less than half the size of the oldest of the last 3. "
                    "Investigate possible data loss."
                )


def _parse_size_bytes(size_str: str) -> int | None:
    """Parse a human-readable size string into bytes.

    Handles formats like '1.5G', '200M', '4096', '512K'.
    Returns None if unparseable.
    """
    if not size_str or size_str == "unknown":
        return None

    size_str = size_str.strip()
    match = re.match(r"^([\d.]+)\s*([KMGTP]?)i?[Bb]?$", size_str, re.IGNORECASE)
    if not match:
        # Try plain integer (bytes)
        try:
            return int(size_str)
        except ValueError:
            return None

    value = float(match.group(1))
    suffix = match.group(2).upper()
    multipliers = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}
    return int(value * multipliers.get(suffix, 1))


def _parse_backup_date(date_str: str) -> datetime | None:
    """Parse a date string from ls output or backup filenames.

    Tries common formats. Returns None if unparseable.
    """
    if not date_str:
        return None

    date_str = date_str.strip()
    formats = [
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%b %d %H:%M",
        "%b %d %Y",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            # If year is 1900 (format without year), assume current year
            if dt.year == 1900:
                dt = dt.replace(year=datetime.now(timezone.utc).year)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _parse_ls_output(raw: str) -> list[dict[str, str]]:
    """Parse 'ls -lhS' or 'ls -lt' output into structured entries.

    Returns a list of dicts with 'name', 'size', 'date' keys.
    """
    entries: list[dict[str, str]] = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("total"):
            continue
        # ls -l format: perms links owner group size month day time/year name
        parts = line.split(None, 8)
        if len(parts) < 9:
            continue
        size = parts[4]
        date_str = f"{parts[5]} {parts[6]} {parts[7]}"
        name = parts[8]
        entries.append({"name": name, "size": size, "date": date_str})
    return entries


class BackupAudit(BaseTool):
    """Audit backup health across servers.

    Checks backup recency, sizes, storage space, and configuration for
    cPanel, Pterodactyl/Pelican, and MySQL/MariaDB backup systems.
    Optionally verifies archive integrity with tar tests.
    """

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "backup_audit"

    @property
    def description(self) -> str:
        return (
            "Audit backup health on a server. Checks backup recency, sizes, "
            "storage space, and configuration for cPanel, Pterodactyl, and MySQL. "
            "Set check_integrity=true to verify archive integrity (slower). "
            "Use backup_type to limit scope: 'cpanel', 'pterodactyl', 'mysql', or 'all'."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server name from the inventory.",
                },
                "check_integrity": {
                    "type": "boolean",
                    "description": (
                        "Run tar -tzf on recent backup archives to verify integrity. "
                        "Default false (slower operation)."
                    ),
                    "default": False,
                },
                "backup_type": {
                    "type": "string",
                    "description": (
                        "Which backup system to audit: 'cpanel', 'pterodactyl', "
                        "'mysql', or 'all' (default)."
                    ),
                    "enum": ["cpanel", "pterodactyl", "mysql", "all"],
                    "default": "all",
                },
            },
            "required": ["server"],
        }

    async def execute(
        self,
        *,
        server: str,
        check_integrity: bool = False,
        backup_type: str = "all",
        **kwargs: Any,
    ) -> ToolResult:
        """Run backup audit on the specified server."""
        try:
            self._inventory.get_server(server)
        except KeyError as e:
            return ToolResult(error=str(e), exit_code=1)

        data: dict[str, Any] = {}

        # Always check storage space on backup-relevant partitions
        storage_result = await _run_on_server(
            self._inventory, server, "df -h /backup /srv /var /tmp"
        )
        data["storage"] = storage_result.output if storage_result.success else storage_result.error

        # Build audit tasks based on backup_type
        tasks: list[tuple[str, Any]] = []

        if backup_type in ("cpanel", "all"):
            tasks.append(("cpanel", self._audit_cpanel(server)))
            tasks.append(("cpanel_config", self._audit_cpanel_config(server)))
            tasks.append(("jetbackup", self._audit_jetbackup(server)))

        if backup_type in ("pterodactyl", "all"):
            tasks.append(("pterodactyl", self._audit_pterodactyl(server)))

        if backup_type in ("mysql", "all"):
            tasks.append(("mysql", self._audit_mysql(server)))

        # Run all audit checks in parallel
        if tasks:
            keys = [t[0] for t in tasks]
            coros = [t[1] for t in tasks]
            results = await asyncio.gather(*coros, return_exceptions=True)
            for key, result in zip(keys, results):
                if isinstance(result, Exception):
                    data[key] = {"error": str(result)}
                else:
                    data[key] = result

        # Optional integrity checks
        if check_integrity:
            data["integrity"] = await self._check_integrity(server, data)

        report = _build_backup_report(server, data)
        return ToolResult(output=report, exit_code=0)

    async def _audit_cpanel(self, server: str) -> dict[str, Any]:
        """Check cPanel backup directories for recent backups."""
        # Try multiple common cPanel backup paths
        paths = [
            "/backup/cpbackup/daily",
            "/backup/cpbackup/weekly",
            "/backup/cpbackup/monthly",
            "/backup",
        ]

        all_files: list[dict[str, str]] = []
        found_any = False

        for path in paths:
            result = await _run_on_server(
                self._inventory, server, f"ls -lht {path}"
            )
            if result.success and result.output.strip():
                found_any = True
                parsed = _parse_ls_output(result.output)
                # Tag entries with their source path
                for entry in parsed:
                    entry["name"] = f"{path}/{entry['name']}"
                all_files.extend(parsed)

        if not found_any:
            return {"not_found": True}

        # Return the 5 most recent entries (ls -lt sorts by time)
        return {"files": all_files[:5]}

    async def _audit_cpanel_config(self, server: str) -> str:
        """Retrieve cPanel backup configuration."""
        # Try whmapi1 first, fall back to config file
        result = await _run_on_server(
            self._inventory, server, "whmapi1 backup_config_get"
        )
        if result.success and result.output.strip():
            return result.output

        result = await _run_on_server(
            self._inventory, server, "cat /var/cpanel/backups/config"
        )
        if result.success and result.output.strip():
            return result.output

        return "cPanel backup config not found"

    async def _audit_jetbackup(self, server: str) -> dict[str, Any]:
        """Check if JetBackup 5 is installed and get basic status."""
        result = await _run_on_server(
            self._inventory, server, "ls /usr/local/jetapps/etc/jetbackup5/"
        )
        if result.success and result.output.strip():
            return {"installed": True, "details": result.output}
        return {"installed": False}

    async def _audit_pterodactyl(self, server: str) -> dict[str, Any]:
        """Check Pterodactyl/Wings backup directory."""
        # Try standard Wings backup path, then fallback
        paths = [
            "/srv/pterodactyl/backups",
            "/srv/pelican/backups",
        ]

        for path in paths:
            result = await _run_on_server(
                self._inventory, server, f"ls -lhS {path}"
            )
            if result.success and result.output.strip():
                parsed = _parse_ls_output(result.output)
                for entry in parsed:
                    entry["name"] = f"{path}/{entry['name']}"
                return {"files": parsed[:5]}

        # Also check Docker volumes for backup data
        result = await _run_on_server(
            self._inventory, server,
            "docker volume ls --format '{{.Name}}' --filter name=backup"
        )
        docker_volumes = ""
        if result.success and result.output.strip():
            docker_volumes = f"Docker backup volumes: {result.output}"

        if docker_volumes:
            return {"files": [], "error": None, "note": docker_volumes}

        return {"not_found": True}

    async def _audit_mysql(self, server: str) -> dict[str, Any]:
        """Check MySQL/MariaDB backup status."""
        data: dict[str, Any] = {}

        # Check for mysqldump files in common locations
        dump_paths = [
            "/var/backups",
            "/backup/mysql",
            "/root",
        ]

        dump_cmd = (
            "find " + " ".join(dump_paths) + " -maxdepth 2 "
            "-name '*.sql' -o -name '*.sql.gz' -o -name '*.sql.bz2' -o -name '*.sql.xz'"
        )
        # Use ls -lht on results to get sizes and dates
        result = await _run_on_server(
            self._inventory, server,
            f"{dump_cmd} 2>/dev/null | head -10 | xargs ls -lht 2>/dev/null"
        )

        if result.success and result.output.strip():
            data["files"] = _parse_ls_output(result.output)
        else:
            # Try automysqlbackup directory
            result = await _run_on_server(
                self._inventory, server, "ls -lht /var/lib/automysqlbackup/"
            )
            if result.success and result.output.strip():
                data["files"] = _parse_ls_output(result.output)
            else:
                data["files"] = []

        # Check if binary logging is enabled (for point-in-time recovery)
        binlog_result = await _run_on_server(
            self._inventory, server,
            "mysql -Bse \"SHOW VARIABLES LIKE 'log_bin'\" 2>/dev/null"
        )
        if binlog_result.success and binlog_result.output.strip():
            data["binlog"] = binlog_result.output.strip()
        else:
            data["binlog"] = "unable to query (mysql not accessible or not installed)"

        return data

    async def _check_integrity(
        self, server: str, data: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Verify integrity of recent backup archives using tar -tzf.

        Only tests .tar.gz, .tgz, and .gz files found in the audit data.
        """
        checks: list[dict[str, Any]] = []
        archive_files: list[str] = []

        # Collect archive files from all backup sections
        for section_key in ("cpanel", "pterodactyl", "mysql"):
            section = data.get(section_key)
            if not isinstance(section, dict):
                continue
            for entry in section.get("files", []):
                name = entry.get("name", "")
                if re.search(r"\.(tar\.gz|tgz|gz)$", name, re.IGNORECASE):
                    archive_files.append(name)

        # Test up to 3 archives to avoid long waits
        for archive_path in archive_files[:3]:
            result = await _run_on_server(
                self._inventory, server,
                f"tar -tzf {archive_path} > /dev/null 2>&1 && echo OK || echo FAILED"
            )
            if result.success and "OK" in result.output:
                checks.append({"file": archive_path, "valid": True})
            else:
                checks.append({
                    "file": archive_path,
                    "valid": False,
                    "error": result.error or result.output,
                })

        return checks
