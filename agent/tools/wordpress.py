"""WordPress management tools via WP-CLI.

Discovers WordPress installations, checks site health, plugin
status, database integrity, cron health, security scanning,
and performance diagnostics. Designed for cPanel servers where
WP-CLI is typically at /usr/local/bin/wp.

All commands run as the account user via ``runuser`` to respect
file ownership.
"""

from __future__ import annotations

import json
import re
from typing import Any

from agent.inventory import Inventory
from agent.tools.base import BaseTool, ToolResult
from agent.tools.docker_tools import _run_on_server


class WpSites(BaseTool):
    """Discover WordPress installations on a server."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "wp_sites"

    @property
    def description(self) -> str:
        return "Find WordPress installations on a cPanel server. Scans /home/*/public_html."

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
        """Find WordPress installs by looking for wp-config.php."""
        cmd = "find /home/*/public_html -maxdepth 2 -name wp-config.php -type f 2>/dev/null"
        result = await _run_on_server(self._inventory, server, cmd)
        if not result.success:
            return result

        if not result.output.strip():
            return ToolResult(output="No WordPress installations found.")

        paths = result.output.strip().splitlines()
        lines = [f"Found {len(paths)} WordPress installation(s):"]
        for path in paths:
            # Extract username from /home/<user>/public_html/...
            parts = path.split("/")
            user = parts[2] if len(parts) > 2 else "?"
            wp_dir = "/".join(parts[:-1])
            lines.append(f"  {user}: {wp_dir}")

        return ToolResult(output="\n".join(lines))


class WpHealth(BaseTool):
    """Run a WordPress site health check."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "wp_health"

    @property
    def description(self) -> str:
        return "WP-CLI site health: core version, database status, update availability."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server name.",
                },
                "path": {
                    "type": "string",
                    "description": "WordPress install path (e.g. /home/user/public_html).",
                },
                "user": {
                    "type": "string",
                    "description": "System user who owns the WP install.",
                },
            },
            "required": ["server", "path", "user"],
        }

    async def execute(
        self, *, server: str, path: str, user: str, **kwargs: Any,
    ) -> ToolResult:
        """Run comprehensive health check via WP-CLI."""
        checks = [
            ("Core version", f"wp core version --path={path}"),
            ("Core update", f"wp core check-update --path={path} --format=table"),
            ("Database", f"wp db check --path={path} 2>&1 | tail -5"),
            ("Plugins needing update", f"wp plugin list --path={path} --update=available --format=table"),
            ("Active theme", f"wp theme list --path={path} --status=active --format=table"),
            ("Site URL", f"wp option get siteurl --path={path}"),
            ("PHP errors", f"wp eval 'echo php_ini_loaded_file();' --path={path}"),
        ]

        results: list[str] = []
        for label, wp_cmd in checks:
            cmd = f"runuser -u {user} -- {wp_cmd}"
            result = await _run_on_server(self._inventory, server, cmd)
            output = result.output.strip() if result.success else f"ERROR: {result.error}"
            results.append(f"**{label}:**\n{output}")

        return ToolResult(output="\n\n".join(results))


class WpPluginStatus(BaseTool):
    """List WordPress plugins with status and update info."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "wp_plugin_status"

    @property
    def description(self) -> str:
        return "List all WP plugins with active/inactive status and available updates."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server name.",
                },
                "path": {
                    "type": "string",
                    "description": "WordPress install path.",
                },
                "user": {
                    "type": "string",
                    "description": "System user who owns the WP install.",
                },
            },
            "required": ["server", "path", "user"],
        }

    async def execute(
        self, *, server: str, path: str, user: str, **kwargs: Any,
    ) -> ToolResult:
        """List plugins via WP-CLI."""
        cmd = (
            f"runuser -u {user} -- "
            f"wp plugin list --path={path} --format=table "
            f"--fields=name,status,update,version,update_version"
        )
        return await _run_on_server(self._inventory, server, cmd)


class WpCoreUpdate(BaseTool):
    """Check if WordPress core needs updating."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "wp_core_update"

    @property
    def description(self) -> str:
        return "Check WordPress core version and available updates."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server name.",
                },
                "path": {
                    "type": "string",
                    "description": "WordPress install path.",
                },
                "user": {
                    "type": "string",
                    "description": "System user who owns the WP install.",
                },
            },
            "required": ["server", "path", "user"],
        }

    async def execute(
        self, *, server: str, path: str, user: str, **kwargs: Any,
    ) -> ToolResult:
        """Check core update status."""
        cmd = f"runuser -u {user} -- wp core check-update --path={path} --format=table"
        return await _run_on_server(self._inventory, server, cmd)


class WpDbCheck(BaseTool):
    """Check WordPress database integrity."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "wp_db_check"

    @property
    def description(self) -> str:
        return "Run WordPress database integrity check and report table sizes."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server name.",
                },
                "path": {
                    "type": "string",
                    "description": "WordPress install path.",
                },
                "user": {
                    "type": "string",
                    "description": "System user who owns the WP install.",
                },
            },
            "required": ["server", "path", "user"],
        }

    async def execute(
        self, *, server: str, path: str, user: str, **kwargs: Any,
    ) -> ToolResult:
        """Check database and report sizes."""
        # Check integrity
        check_cmd = f"runuser -u {user} -- wp db check --path={path}"
        check = await _run_on_server(self._inventory, server, check_cmd)

        # Get table sizes
        size_cmd = f"runuser -u {user} -- wp db size --path={path} --format=table --all-tables"
        sizes = await _run_on_server(self._inventory, server, size_cmd)

        parts = []
        parts.append("**Integrity Check:**")
        parts.append(check.output if check.success else f"ERROR: {check.error}")
        parts.append("\n**Table Sizes:**")
        parts.append(sizes.output if sizes.success else f"ERROR: {sizes.error}")

        return ToolResult(output="\n".join(parts))


class WpCronStatus(BaseTool):
    """Check WordPress cron health."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "wp_cron_status"

    @property
    def description(self) -> str:
        return "Check WP-Cron: pending events, overdue jobs, and cron type."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server name.",
                },
                "path": {
                    "type": "string",
                    "description": "WordPress install path.",
                },
                "user": {
                    "type": "string",
                    "description": "System user who owns the WP install.",
                },
            },
            "required": ["server", "path", "user"],
        }

    async def execute(
        self, *, server: str, path: str, user: str, **kwargs: Any,
    ) -> ToolResult:
        """Check WP-Cron status."""
        cmd = f"runuser -u {user} -- wp cron event list --path={path} --format=table"
        return await _run_on_server(self._inventory, server, cmd)


class WpSearchReplace(BaseTool):
    """Dry-run a WordPress search-replace (for migrations)."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "wp_search_replace_dry"

    @property
    def description(self) -> str:
        return "Dry-run search-replace in WP database. Shows what would change without modifying data."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server name.",
                },
                "path": {
                    "type": "string",
                    "description": "WordPress install path.",
                },
                "user": {
                    "type": "string",
                    "description": "System user who owns the WP install.",
                },
                "old_value": {
                    "type": "string",
                    "description": "String to search for.",
                },
                "new_value": {
                    "type": "string",
                    "description": "String to replace with.",
                },
            },
            "required": ["server", "path", "user", "old_value", "new_value"],
        }

    async def execute(
        self,
        *,
        server: str,
        path: str,
        user: str,
        old_value: str,
        new_value: str,
        **kwargs: Any,
    ) -> ToolResult:
        """Dry-run search-replace."""
        cmd = (
            f"runuser -u {user} -- "
            f"wp search-replace '{old_value}' '{new_value}' "
            f"--path={path} --dry-run --report-changed-only"
        )
        return await _run_on_server(self._inventory, server, cmd)


# ── Security Scanning ────────────────────────────────────────────


class WpSecurityScan(BaseTool):
    """Scan a WordPress install for common security issues."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "wp_security_scan"

    @property
    def description(self) -> str:
        return (
            "Scan WordPress for security issues: PHP files in uploads, "
            "modified core files, world-writable files, obfuscated code, "
            "and core file integrity via WP-CLI checksum."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server name.",
                },
                "path": {
                    "type": "string",
                    "description": "WordPress install path.",
                },
                "user": {
                    "type": "string",
                    "description": "System user who owns the WP install.",
                },
            },
            "required": ["server", "path", "user"],
        }

    async def execute(
        self, *, server: str, path: str, user: str, **kwargs: Any,
    ) -> ToolResult:
        """Run security checks."""
        checks = {
            # Core file integrity — compares checksums against wordpress.org
            "core_verify": (
                f"runuser -u {user} -- "
                f"wp core verify-checksums --path={path} 2>&1"
            ),
            # PHP files in uploads directory — almost always malware
            "php_in_uploads": (
                f"find {path}/wp-content/uploads -name '*.php' "
                f"-o -name '*.phtml' -o -name '*.php5' -o -name '*.phar' "
                f"2>/dev/null"
            ),
            # World-writable files — shouldn't exist
            "world_writable": (
                f"find {path} -type f -perm -o+w "
                f"-not -path '*/node_modules/*' 2>/dev/null"
            ),
            # Obfuscated code signatures in theme/plugin files
            "obfuscated": (
                f"grep -rlc "
                f"'base64_decode\\|eval(\\|assert(\\|create_function\\|"
                f"str_rot13\\|gzinflate\\|gzuncompress\\|preg_replace.*e.' "
                f"{path}/wp-content/ 2>/dev/null"
            ),
            # Suspicious .htaccess files outside root
            "htaccess": (
                f"find {path}/wp-content -name '.htaccess' 2>/dev/null"
            ),
            # Recently modified files (last 3 days) in wp-includes/wp-admin
            # These should rarely change outside of updates
            "recent_core_changes": (
                f"find {path}/wp-includes {path}/wp-admin "
                f"-type f -name '*.php' -mtime -3 2>/dev/null"
            ),
        }

        results: dict[str, str] = {}
        for label, cmd in checks.items():
            result = await _run_on_server(self._inventory, server, cmd)
            results[label] = result.output.strip() if result.success else f"ERROR: {result.error}"

        return ToolResult(output=_format_security_scan(results))


class WpFileIntegrity(BaseTool):
    """Check WordPress core file integrity via checksums."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "wp_file_integrity"

    @property
    def description(self) -> str:
        return "Verify WP core files against official checksums. Detects modified or injected files."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {"type": "string", "description": "Server name."},
                "path": {"type": "string", "description": "WordPress install path."},
                "user": {"type": "string", "description": "System user."},
            },
            "required": ["server", "path", "user"],
        }

    async def execute(
        self, *, server: str, path: str, user: str, **kwargs: Any,
    ) -> ToolResult:
        """Verify core checksums."""
        cmd = f"runuser -u {user} -- wp core verify-checksums --path={path}"
        return await _run_on_server(self._inventory, server, cmd)


# ── Performance Diagnostics ──────────────────────────────────────


class WpPerformance(BaseTool):
    """WordPress performance diagnostics."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "wp_performance"

    @property
    def description(self) -> str:
        return (
            "WP performance diagnostics: object cache status, page cache "
            "detection, database bloat (revisions, spam, transients), "
            "autoloaded option size, and PHP memory limits."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {"type": "string", "description": "Server name."},
                "path": {"type": "string", "description": "WordPress install path."},
                "user": {"type": "string", "description": "System user."},
            },
            "required": ["server", "path", "user"],
        }

    async def execute(
        self, *, server: str, path: str, user: str, **kwargs: Any,
    ) -> ToolResult:
        """Run performance diagnostics."""
        wp = f"runuser -u {user} -- wp --path={path}"
        checks = {
            # Object cache type (Redis, Memcached, or none)
            "Object cache": (
                f"{wp} eval "
                "'if(wp_using_ext_object_cache()){{echo \"External: \".get_class($GLOBALS[\"wp_object_cache\"]);}}else{{echo \"None (built-in)\";}}'"
            ),
            # Page caching detection
            "Page cache plugins": (
                f"{wp} plugin list --status=active --format=csv --fields=name "
                "2>/dev/null"
            ),
            # Autoloaded options size (performance killer when too large)
            "Autoloaded options": (
                f"{wp} db query "
                "\"SELECT SUM(LENGTH(option_value)) as bytes FROM wp_options WHERE autoload='yes'\" "
                "--skip-column-names"
            ),
            # Post revisions count
            "Post revisions": (
                f"{wp} post list --post_type=revision --format=count"
            ),
            # Spam comments
            "Spam comments": (
                f"{wp} comment list --status=spam --format=count"
            ),
            # Trashed comments
            "Trashed comments": (
                f"{wp} comment list --status=trash --format=count"
            ),
            # Trashed posts
            "Trashed posts": (
                f"{wp} post list --post_status=trash --format=count"
            ),
            # Transients count (stale transients = bloat)
            "Transients": (
                f"{wp} db query "
                "\"SELECT COUNT(*) FROM wp_options WHERE option_name LIKE '_transient_%'\" "
                "--skip-column-names"
            ),
            # PHP memory limit
            "PHP memory_limit": (
                f"{wp} eval 'echo ini_get(\"memory_limit\");'"
            ),
            # PHP max_execution_time
            "PHP max_execution_time": (
                f"{wp} eval 'echo ini_get(\"max_execution_time\");'"
            ),
            # Total DB size
            "Total DB size": (
                f"{wp} db size --format=csv --skip-column-names"
            ),
        }

        parts: list[str] = []
        for label, cmd in checks.items():
            result = await _run_on_server(self._inventory, server, cmd)
            val = result.output.strip() if result.success else f"ERROR"

            # Post-process specific checks
            if label == "Page cache plugins" and result.success:
                cache_plugins = [
                    p for p in val.splitlines()
                    if any(kw in p.lower() for kw in (
                        "cache", "rocket", "w3-total", "super-cache",
                        "fastest", "breeze", "litespeed", "swift",
                        "hummingbird", "autoptimize", "cloudflare",
                    ))
                ]
                val = ", ".join(cache_plugins) if cache_plugins else "None detected"
            elif label == "Autoloaded options" and result.success:
                try:
                    bytes_val = int(val.strip())
                    mb = bytes_val / (1024 * 1024)
                    val = f"{mb:.1f} MB"
                    if mb > 1.0:
                        val += " ⚠ (should be < 1 MB)"
                except ValueError:
                    pass

            parts.append(f"{label}: {val}")

        return ToolResult(output="\n".join(parts))


class WpCleanupDry(BaseTool):
    """Preview what a WordPress cleanup would remove."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "wp_cleanup_preview"

    @property
    def description(self) -> str:
        return (
            "Preview cleanup: counts of revisions, spam, trash, and "
            "transients that could be purged. Read-only, no changes made."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {"type": "string", "description": "Server name."},
                "path": {"type": "string", "description": "WordPress install path."},
                "user": {"type": "string", "description": "System user."},
            },
            "required": ["server", "path", "user"],
        }

    async def execute(
        self, *, server: str, path: str, user: str, **kwargs: Any,
    ) -> ToolResult:
        """Preview cleanup counts."""
        wp = f"runuser -u {user} -- wp --path={path}"
        items = {
            "Post revisions": f"{wp} post list --post_type=revision --format=count",
            "Auto-drafts": f"{wp} post list --post_status=auto-draft --format=count",
            "Trashed posts": f"{wp} post list --post_status=trash --format=count",
            "Spam comments": f"{wp} comment list --status=spam --format=count",
            "Trashed comments": f"{wp} comment list --status=trash --format=count",
            "Expired transients": (
                f"{wp} db query "
                "\"SELECT COUNT(*) FROM wp_options WHERE option_name LIKE '_transient_timeout_%' "
                "AND option_value < UNIX_TIMESTAMP()\" --skip-column-names"
            ),
        }

        lines = ["**Cleanup Preview (nothing will be deleted):**"]
        total = 0
        for label, cmd in items.items():
            result = await _run_on_server(self._inventory, server, cmd)
            try:
                count = int(result.output.strip()) if result.success else 0
            except ValueError:
                count = 0
            total += count
            if count > 0:
                lines.append(f"  {label}: {count}")
            else:
                lines.append(f"  {label}: 0")

        lines.append(f"\nTotal removable items: {total}")
        if total > 1000:
            lines.append("⚠ Significant bloat detected — cleanup recommended")

        return ToolResult(output="\n".join(lines))


# ── Formatters ───────────────────────────────────────────────────


def _format_security_scan(results: dict[str, str]) -> str:
    """Format security scan results into a clear report."""
    parts: list[str] = []
    issues = 0

    # Core integrity
    core = results.get("core_verify", "")
    if "success" in core.lower() or "no issues" in core.lower() or not core:
        parts.append("✓ Core integrity: OK (checksums match)")
    elif core.startswith("ERROR"):
        parts.append(f"⚠ Core integrity: could not verify ({core})")
    else:
        parts.append(f"✗ Core integrity: MODIFIED FILES DETECTED")
        for line in core.splitlines()[:10]:
            parts.append(f"    {line}")
        issues += 1

    # PHP in uploads
    php_uploads = results.get("php_in_uploads", "")
    if php_uploads and not php_uploads.startswith("ERROR"):
        files = [f for f in php_uploads.splitlines() if f.strip()]
        if files:
            parts.append(f"✗ PHP files in uploads: {len(files)} found (likely malware)")
            for f in files[:10]:
                parts.append(f"    {f}")
            if len(files) > 10:
                parts.append(f"    ... and {len(files) - 10} more")
            issues += 1
        else:
            parts.append("✓ Uploads directory: clean (no PHP files)")
    else:
        parts.append("✓ Uploads directory: clean (no PHP files)")

    # World-writable files
    ww = results.get("world_writable", "")
    if ww and not ww.startswith("ERROR"):
        files = [f for f in ww.splitlines() if f.strip()]
        if files:
            parts.append(f"⚠ World-writable files: {len(files)} found")
            for f in files[:5]:
                parts.append(f"    {f}")
            issues += 1
        else:
            parts.append("✓ File permissions: no world-writable files")
    else:
        parts.append("✓ File permissions: no world-writable files")

    # Obfuscated code
    obf = results.get("obfuscated", "")
    if obf and not obf.startswith("ERROR"):
        files = [f for f in obf.splitlines() if f.strip()]
        if files:
            parts.append(f"⚠ Obfuscated code patterns: {len(files)} file(s)")
            for f in files[:10]:
                parts.append(f"    {f}")
            issues += 1
        else:
            parts.append("✓ No obfuscated code patterns detected")
    else:
        parts.append("✓ No obfuscated code patterns detected")

    # Suspicious .htaccess
    htaccess = results.get("htaccess", "")
    if htaccess and not htaccess.startswith("ERROR"):
        files = [f for f in htaccess.splitlines() if f.strip()]
        if files:
            parts.append(f"⚠ .htaccess in wp-content: {len(files)} file(s)")
            for f in files[:5]:
                parts.append(f"    {f}")
            issues += 1
        else:
            parts.append("✓ No suspicious .htaccess files")
    else:
        parts.append("✓ No suspicious .htaccess files")

    # Recently modified core files
    recent = results.get("recent_core_changes", "")
    if recent and not recent.startswith("ERROR"):
        files = [f for f in recent.splitlines() if f.strip()]
        if files:
            parts.append(f"⚠ Core files modified in last 3 days: {len(files)}")
            for f in files[:5]:
                parts.append(f"    {f}")
            issues += 1
        else:
            parts.append("✓ No recent core file modifications")
    else:
        parts.append("✓ No recent core file modifications")

    # Summary
    header = f"**Security Scan: {issues} issue(s) found**\n" if issues else "**Security Scan: All clear**\n"
    return header + "\n".join(parts)
