"""Systemd service tools: status and journal reads.

Works locally and on remote servers. Commands are built
programmatically from validated service names.
"""

from __future__ import annotations

import asyncio
import shlex
from typing import Any

from agent.inventory import Inventory
from agent.tools.base import BaseTool, ToolResult


class ServiceStatus(BaseTool):
    """Check systemd service status on a server."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "service_status"

    @property
    def description(self) -> str:
        return "Check the status of a systemd service on a server."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server name from the inventory.",
                },
                "service": {
                    "type": "string",
                    "description": "Systemd service name (e.g. 'docker', 'nginx').",
                },
            },
            "required": ["server", "service"],
        }

    async def execute(self, *, server: str, service: str, **kwargs: Any) -> ToolResult:
        """Check service status."""
        command = f"systemctl status {service}"
        return await _run_on_server(self._inventory, server, command)


class ServiceJournal(BaseTool):
    """Read systemd journal for a service."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "service_journal"

    @property
    def description(self) -> str:
        return (
            "Read the systemd journal (logs) for a service on a server. "
            "Optionally limit by number of lines or time range."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server name from the inventory.",
                },
                "service": {
                    "type": "string",
                    "description": "Systemd service name.",
                },
                "lines": {
                    "type": "integer",
                    "description": "Number of journal lines to return (default 50).",
                    "default": 50,
                },
                "since": {
                    "type": "string",
                    "description": "Show entries since this time (e.g. '1h ago', 'today', '2024-01-01').",
                },
            },
            "required": ["server", "service"],
        }

    async def execute(
        self,
        *,
        server: str,
        service: str,
        lines: int = 50,
        since: str | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        """Read service journal."""
        command = f"journalctl -u {service} --no-pager -n {lines}"
        if since:
            command += f" --since '{since}'"

        return await _run_on_server(self._inventory, server, command)


async def _run_on_server(inventory: Inventory, server: str, command: str) -> ToolResult:
    """Run a command locally or remotely depending on the server."""
    try:
        server_info = inventory.get_server(server)
    except KeyError as e:
        return ToolResult(error=str(e), exit_code=1)

    if server == "localhost" or not server_info.definition.ssh:
        return await _run_local(command)

    from agent.tools.remote import run_remote_command
    return await run_remote_command(server_info, command)


async def _run_local(command: str) -> ToolResult:
    """Run a command locally using subprocess."""
    args = shlex.split(command)

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    except FileNotFoundError:
        return ToolResult(error=f"Command not found: {args[0]}", exit_code=127)

    return ToolResult(
        output=stdout.decode("utf-8", errors="replace").rstrip(),
        error=stderr.decode("utf-8", errors="replace").rstrip(),
        exit_code=proc.returncode or 0,
    )
