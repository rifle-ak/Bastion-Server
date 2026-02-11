"""SSH command execution on downstream servers.

Uses asyncssh for all remote operations — never shells out to ssh.
Each server uses its own dedicated keypair from the inventory config.
"""

from __future__ import annotations

import asyncio
from typing import Any

import asyncssh
import structlog

from agent.inventory import Inventory, ServerInfo
from agent.tools.base import BaseTool, ToolResult

logger = structlog.get_logger()


async def run_remote_command(
    server_info: ServerInfo,
    command: str,
    timeout: int = 30,
) -> ToolResult:
    """Execute a command on a remote server via SSH.

    Args:
        server_info: Resolved server info with connection details.
        command: The command to execute.
        timeout: Execution timeout in seconds.

    Returns:
        ToolResult with stdout, stderr, and exit code.
    """
    defn = server_info.definition

    if not defn.ssh:
        return ToolResult(
            error=f"Server {server_info.name!r} does not use SSH (local execution only).",
            exit_code=1,
        )

    if not defn.key_path:
        return ToolResult(
            error=f"No SSH key configured for server {server_info.name!r}.",
            exit_code=1,
        )

    try:
        async with asyncssh.connect(
            defn.host,
            username=defn.user,
            client_keys=[defn.key_path],
            known_hosts=defn.known_hosts_path,
        ) as conn:
            result = await asyncio.wait_for(
                conn.run(command, check=False),
                timeout=timeout,
            )
            return ToolResult(
                output=(result.stdout or "").rstrip(),
                error=(result.stderr or "").rstrip(),
                exit_code=result.exit_status or 0,
            )
    except asyncssh.DisconnectError as e:
        logger.error("ssh_disconnect", server=server_info.name, error=str(e))
        return ToolResult(error=f"SSH disconnected: {e}", exit_code=1)
    except asyncssh.PermissionDenied as e:
        logger.error("ssh_permission_denied", server=server_info.name, error=str(e))
        return ToolResult(error=f"SSH permission denied: {e}", exit_code=1)
    except OSError as e:
        logger.error("ssh_connection_failed", server=server_info.name, error=str(e))
        return ToolResult(error=f"SSH connection failed: {e}", exit_code=1)
    except asyncio.TimeoutError:
        return ToolResult(error=f"Command timed out after {timeout}s", exit_code=1)


class RunRemoteCommand(BaseTool):
    """Execute a command on a downstream server via SSH."""

    def __init__(self, inventory: Inventory, timeout: int = 30) -> None:
        self._inventory = inventory
        self._timeout = timeout

    @property
    def name(self) -> str:
        return "run_remote_command"

    @property
    def description(self) -> str:
        return (
            "Execute a command on a downstream server via SSH. The server must "
            "exist in the inventory and the command must be on that server's "
            "role allowlist. Destructive commands require operator approval."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server name from the inventory (e.g. 'gameserver-01', 'monitoring').",
                },
                "command": {
                    "type": "string",
                    "description": "The command to execute on the remote server.",
                },
            },
            "required": ["server", "command"],
        }

    async def execute(self, *, server: str, command: str, **kwargs: Any) -> ToolResult:
        """Execute a command on a remote server.

        Args:
            server: Server name from inventory.
            command: Command to execute.

        Returns:
            ToolResult with command output.
        """
        try:
            server_info = self._inventory.get_server(server)
        except KeyError as e:
            return ToolResult(error=str(e), exit_code=1)

        # Local servers don't use SSH
        if not server_info.definition.ssh:
            return ToolResult(
                error=f"Server {server!r} is local. Use run_local_command instead.",
                exit_code=1,
            )

        return await run_remote_command(server_info, command, self._timeout)
