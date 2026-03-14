"""WordPress management tools via WP-CLI.

Discovers WordPress installations, checks site health, plugin
status, database integrity, and cron health. Designed for cPanel
servers where WP-CLI is typically at /usr/local/bin/wp.

All commands run as the account user via `su - <user> -c '...'`
or via `runuser` to respect file ownership.
"""

from __future__ import annotations

import json
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
