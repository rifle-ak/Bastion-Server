"""Local command execution on the bastion server.

Uses asyncio.create_subprocess_exec — never shell=True. Commands are
split with shlex and executed directly. The security pipeline in the
registry handles sanitization and allowlist checks before we get here.
"""

from __future__ import annotations

import asyncio
import shlex
from typing import Any

from agent.tools.base import BaseTool, ToolResult


class RunLocalCommand(BaseTool):
    """Execute a command on the bastion server itself."""

    @property
    def name(self) -> str:
        return "run_local_command"

    @property
    def description(self) -> str:
        return (
            "Execute a shell command on the bastion server (this machine). "
            "Only commands matching the bastion allowlist are permitted. "
            "Destructive commands require operator approval."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The command to execute (e.g. 'uptime', 'df -h', 'docker ps').",
                },
            },
            "required": ["command"],
        }

    async def execute(self, *, command: str, **kwargs: Any) -> ToolResult:
        """Execute a local command using subprocess_exec.

        Args:
            command: The command string to execute.

        Returns:
            ToolResult with stdout, stderr, and exit code.
        """
        try:
            args = shlex.split(command)
        except ValueError as e:
            return ToolResult(error=f"Invalid command syntax: {e}", exit_code=1)

        if not args:
            return ToolResult(error="Empty command", exit_code=1)

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
        except FileNotFoundError:
            return ToolResult(error=f"Command not found: {args[0]}", exit_code=127)
        except PermissionError:
            return ToolResult(error=f"Permission denied: {args[0]}", exit_code=126)

        return ToolResult(
            output=stdout.decode("utf-8", errors="replace").rstrip(),
            error=stderr.decode("utf-8", errors="replace").rstrip(),
            exit_code=proc.returncode or 0,
        )
