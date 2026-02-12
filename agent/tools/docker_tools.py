"""Docker container tools: ps and logs.

Works locally and on remote servers. Commands are built
programmatically — no raw shell strings from the model.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent.inventory import Inventory
from agent.tools.base import BaseTool, ToolResult


class DockerPs(BaseTool):
    """List running Docker containers on a server."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "docker_ps"

    @property
    def description(self) -> str:
        return (
            "List running Docker containers on a server. Set 'all' to true "
            "to include stopped containers."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server name from the inventory.",
                },
                "all": {
                    "type": "boolean",
                    "description": "Include stopped containers (default false).",
                    "default": False,
                },
            },
            "required": ["server"],
        }

    async def execute(self, *, server: str, all: bool = False, **kwargs: Any) -> ToolResult:
        """List Docker containers."""
        cmd = "docker ps --format 'table {{.ID}}\\t{{.Names}}\\t{{.Status}}\\t{{.Image}}\\t{{.Ports}}'"
        if all:
            cmd = "docker ps -a --format 'table {{.ID}}\\t{{.Names}}\\t{{.Status}}\\t{{.Image}}\\t{{.Ports}}'"

        return await _run_on_server(self._inventory, server, cmd)


class DockerLogs(BaseTool):
    """Fetch Docker container logs."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "docker_logs"

    @property
    def description(self) -> str:
        return (
            "Fetch logs from a Docker container on a server. "
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
                "container": {
                    "type": "string",
                    "description": "Container name or ID.",
                },
                "lines": {
                    "type": "integer",
                    "description": "Number of log lines to return (default 100).",
                    "default": 100,
                },
                "since": {
                    "type": "string",
                    "description": "Show logs since this time (e.g. '1h', '30m', '2024-01-01').",
                },
            },
            "required": ["server", "container"],
        }

    async def execute(
        self,
        *,
        server: str,
        container: str,
        lines: int = 100,
        since: str | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        """Fetch container logs."""
        # Build command programmatically — never pass raw model strings to shell
        cmd = f"docker logs --tail {lines}"
        if since:
            cmd += f" --since {since}"
        cmd += f" {container}"

        return await _run_on_server(self._inventory, server, cmd)


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
    import shlex
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
