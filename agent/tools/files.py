"""Scoped file read operations.

Reads files on local or remote servers, validated against the role's
allowed_paths_read. Write operations are not implemented in this phase.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent.inventory import Inventory
from agent.tools.base import BaseTool, ToolResult


class ReadFile(BaseTool):
    """Read a file's contents with an optional line limit."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "Read the contents of a file on a server. The path must be within "
            "the allowed read directories for that server's role. Returns up to "
            "'lines' lines from the file. Use server 'localhost' for the bastion."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server name from the inventory (e.g. 'localhost', 'gameserver-01').",
                },
                "path": {
                    "type": "string",
                    "description": "Absolute path to the file to read.",
                },
                "lines": {
                    "type": "integer",
                    "description": "Maximum number of lines to return (default 100).",
                    "default": 100,
                },
            },
            "required": ["server", "path"],
        }

    async def execute(
        self, *, server: str, path: str, lines: int = 100, **kwargs: Any
    ) -> ToolResult:
        """Read a file using head, locally or via SSH.

        Args:
            server: Server name.
            path: Absolute file path.
            lines: Max lines to return.

        Returns:
            ToolResult with file contents or error.
        """
        command = f"head -n {lines} {path}"

        if server == "localhost":
            return await self._read_local(path, lines)

        # Remote read via SSH (lazy import to avoid hard dep on asyncssh)
        try:
            server_info = self._inventory.get_server(server)
        except KeyError as e:
            return ToolResult(error=str(e), exit_code=1)

        from agent.tools.remote import run_remote_command

        return await run_remote_command(server_info, command)

    async def _read_local(self, path: str, lines: int) -> ToolResult:
        """Read a file locally using head."""
        args = ["head", "-n", str(lines), path]

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=30
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return ToolResult(error="File read timed out", exit_code=124)
        except FileNotFoundError:
            return ToolResult(error=f"File not found: {path}", exit_code=1)

        if proc.returncode != 0:
            return ToolResult(
                error=stderr.decode("utf-8", errors="replace").rstrip(),
                exit_code=proc.returncode or 1,
            )

        return ToolResult(
            output=stdout.decode("utf-8", errors="replace").rstrip(),
            exit_code=0,
        )
