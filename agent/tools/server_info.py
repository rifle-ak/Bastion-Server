"""Server inventory and status tools.

list_servers is always safe (read-only, no execution).
get_server_status runs read-only commands to aggregate health info.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent.inventory import Inventory
from agent.tools.base import BaseTool, ToolResult


class ListServers(BaseTool):
    """Return the server inventory with roles and descriptions."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "list_servers"

    @property
    def description(self) -> str:
        return (
            "List all servers in the inventory with their roles, hosts, "
            "and descriptions. No parameters required. Always permitted."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {},
            "required": [],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        """Return formatted server inventory."""
        output = self._inventory.format_for_prompt()
        return ToolResult(output=output, exit_code=0)


class GetServerStatus(BaseTool):
    """Quick health check: uptime, load, disk, memory for a server."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "get_server_status"

    @property
    def description(self) -> str:
        return (
            "Get a quick health summary for a server: uptime, load average, "
            "disk usage, and memory usage. For remote servers, SSH tools "
            "must be available."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server name from the inventory.",
                },
            },
            "required": ["server"],
        }

    async def execute(self, *, server: str, **kwargs: Any) -> ToolResult:
        """Run health check commands and aggregate results.

        Currently only supports localhost. Remote servers will work
        once SSH tools are added.
        """
        try:
            self._inventory.get_server(server)
        except KeyError as e:
            return ToolResult(error=str(e), exit_code=1)

        if server != "localhost":
            return ToolResult(
                error=f"Remote server status not yet implemented (server: {server}). "
                "SSH tools coming in build step 7.",
                exit_code=1,
            )

        # These commands are hardcoded and always safe (read-only).
        # Do NOT add destructive commands here — use the registry
        # dispatch pipeline instead.
        commands = {
            "uptime": ["uptime"],
            "disk": ["df", "-h"],
            "memory": ["free", "-h"],
        }

        sections: list[str] = []
        for label, args in commands.items():
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=10
                    )
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    sections.append(f"=== {label.upper()} ===\nError: timed out")
                    continue
                output = stdout.decode("utf-8", errors="replace").rstrip()
                sections.append(f"=== {label.upper()} ===\n{output}")
            except Exception as e:
                sections.append(f"=== {label.upper()} ===\nError: {e}")

        return ToolResult(output="\n\n".join(sections), exit_code=0)
