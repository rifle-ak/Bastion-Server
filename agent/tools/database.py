"""MySQL/MariaDB diagnostic tools.

Status checks, process lists, slow query detection, and
database-level disk usage. Works on cPanel servers (MySQL root
access via /root/.my.cnf) or any server with mysqladmin/mysql.
"""

from __future__ import annotations

from typing import Any

from agent.inventory import Inventory
from agent.tools.base import BaseTool, ToolResult
from agent.tools.docker_tools import _run_on_server


class MySQLStatus(BaseTool):
    """Check MySQL/MariaDB server status."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "mysql_status"

    @property
    def description(self) -> str:
        return "MySQL status: uptime, connections, threads, queries/sec, buffer pool usage."

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
        """Get MySQL status summary."""
        cmd = "mysqladmin status"
        result = await _run_on_server(self._inventory, server, cmd)
        if not result.success:
            return result

        # Also get key metrics
        extended_cmd = "mysqladmin extended-status"
        ext = await _run_on_server(self._inventory, server, extended_cmd)

        parts = ["**Status:**", result.output]
        if ext.success:
            # Extract key metrics
            metrics = _extract_mysql_metrics(ext.output)
            if metrics:
                parts.append("\n**Key Metrics:**")
                parts.extend(metrics)

        return ToolResult(output="\n".join(parts))


class MySQLProcessList(BaseTool):
    """Show active MySQL queries."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "mysql_processlist"

    @property
    def description(self) -> str:
        return "Show active MySQL queries and connections. Flags long-running queries."

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
        """Show processlist."""
        cmd = "mysqladmin processlist"
        return await _run_on_server(self._inventory, server, cmd)


class MySQLSlowQueries(BaseTool):
    """Check for slow queries."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "mysql_slow_queries"

    @property
    def description(self) -> str:
        return "Show recent slow queries from MySQL slow query log."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server name.",
                },
                "lines": {
                    "type": "integer",
                    "description": "Number of lines from end of log (default 100).",
                    "default": 100,
                },
            },
            "required": ["server"],
        }

    async def execute(
        self, *, server: str, lines: int = 100, **kwargs: Any,
    ) -> ToolResult:
        """Read slow query log."""
        # Common slow query log locations
        for path in [
            "/var/log/mysql/slow-query.log",
            "/var/lib/mysql/slow-query.log",
            "/var/log/mariadb/slow-query.log",
            "/var/log/mysql/mysql-slow.log",
        ]:
            check = await _run_on_server(
                self._inventory, server, f"test -f {path}",
            )
            if check.exit_code == 0:
                cmd = f"tail -n {lines} {path}"
                return await _run_on_server(self._inventory, server, cmd)

        return ToolResult(
            error="Slow query log not found. It may be disabled.",
            exit_code=1,
        )


class MySQLDatabaseSizes(BaseTool):
    """Show database sizes on disk."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "mysql_database_sizes"

    @property
    def description(self) -> str:
        return "Show MySQL database sizes sorted by size. Identifies large databases."

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
        """Get database sizes from information_schema."""
        cmd = "du -sh /var/lib/mysql/*"
        return await _run_on_server(self._inventory, server, cmd)


# ── Helpers ──────────────────────────────────────────────────────


_KEY_VARS = {
    "Threads_connected": "Connected threads",
    "Threads_running": "Running threads",
    "Slow_queries": "Slow queries (total)",
    "Questions": "Total queries",
    "Aborted_connects": "Aborted connections",
    "Aborted_clients": "Aborted clients",
    "Innodb_buffer_pool_pages_free": "InnoDB buffer free pages",
    "Innodb_buffer_pool_pages_total": "InnoDB buffer total pages",
    "Max_used_connections": "Peak connections",
    "Open_tables": "Open tables",
    "Table_locks_waited": "Table lock waits",
}


def _extract_mysql_metrics(extended_output: str) -> list[str]:
    """Extract key metrics from mysqladmin extended-status output."""
    results: list[str] = []
    for line in extended_output.splitlines():
        parts = [p.strip() for p in line.split("|") if p.strip()]
        if len(parts) >= 2:
            var_name = parts[0]
            var_value = parts[1]
            if var_name in _KEY_VARS:
                results.append(f"  {_KEY_VARS[var_name]}: {var_value}")
    return results
