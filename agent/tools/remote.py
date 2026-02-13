"""SSH command execution on downstream servers.

Uses asyncssh for all remote operations — never shells out to ssh.
Each server uses its own dedicated keypair from the inventory config.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import asyncssh
import structlog

from agent.inventory import Inventory, ServerInfo
from agent.tools.base import BaseTool, ToolResult

logger = structlog.get_logger()

# Connection timeout (seconds). Fail fast if the host is unreachable
# rather than eating the entire command_timeout on a TCP SYN hang.
_CONNECT_TIMEOUT = 10


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
    name = server_info.name

    if not defn.ssh:
        return ToolResult(
            error=f"Server {name!r} does not use SSH (local execution only).",
            exit_code=1,
        )

    if not defn.key_path:
        return ToolResult(
            error=f"No SSH key configured for server {name!r}.",
            exit_code=1,
        )

    # Pre-flight: check the SSH key file actually exists
    key_file = Path(defn.key_path)
    if not key_file.exists():
        return ToolResult(
            error=(
                f"SSH key not found: {defn.key_path}\n"
                f"Generate keys: bastion-agent generate-ssh-keys, "
                f"or check key_path in servers.yaml for {name!r}."
            ),
            exit_code=1,
        )

    try:
        # Wrap connect() in its own timeout so we get a clear
        # "cannot reach host" error instead of the generic dispatch
        # timeout after 30s.
        conn = await asyncio.wait_for(
            asyncssh.connect(
                defn.host,
                username=defn.user,
                client_keys=[defn.key_path],
                known_hosts=defn.known_hosts_path,
            ),
            timeout=_CONNECT_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.error("ssh_connect_timeout", server=name, host=defn.host)
        return ToolResult(
            error=(
                f"Cannot connect to {name} ({defn.host}:22) — connection timed out after {_CONNECT_TIMEOUT}s.\n"
                f"Check: Is the IP correct in servers.yaml? Is SSH open on the target? "
                f"Can the bastion reach it?"
            ),
            exit_code=1,
        )
    except asyncssh.PermissionDenied as e:
        logger.error("ssh_permission_denied", server=name, error=str(e))
        return ToolResult(
            error=(
                f"SSH permission denied on {name} ({defn.host}): {e}\n"
                f"Check: Does user {defn.user!r} exist on {name}? "
                f"Is the public key in ~{defn.user}/.ssh/authorized_keys?"
            ),
            exit_code=1,
        )
    except asyncssh.HostKeyNotVerifiable as e:
        logger.error("ssh_host_key_rejected", server=name, error=str(e))
        return ToolResult(
            error=(
                f"SSH host key not trusted for {name} ({defn.host}): {e}\n"
                f"Fix: ssh-keyscan {defn.host} >> ~/.ssh/known_hosts  "
                f"(as the claude-agent user), or set known_hosts_path in servers.yaml."
            ),
            exit_code=1,
        )
    except asyncssh.KeyImportError as e:
        logger.error("ssh_key_error", server=name, error=str(e))
        return ToolResult(
            error=f"SSH key is invalid or corrupt ({defn.key_path}): {e}",
            exit_code=1,
        )
    except asyncssh.DisconnectError as e:
        logger.error("ssh_disconnect", server=name, error=str(e))
        return ToolResult(error=f"SSH disconnected from {name}: {e}", exit_code=1)
    except OSError as e:
        logger.error("ssh_connection_failed", server=name, host=defn.host, error=str(e))
        return ToolResult(
            error=(
                f"Cannot connect to {name} ({defn.host}): {e}\n"
                f"Check: Is the IP correct? Is the server online? Is port 22 open?"
            ),
            exit_code=1,
        )

    # Connection succeeded — run the command
    try:
        async with conn:
            result = await asyncio.wait_for(
                conn.run(command, check=False),
                timeout=timeout,
            )
            return ToolResult(
                output=(result.stdout or "").rstrip(),
                error=(result.stderr or "").rstrip(),
                exit_code=result.exit_status or 0,
            )
    except asyncio.TimeoutError:
        return ToolResult(
            error=f"Command timed out after {timeout}s on {name}",
            exit_code=1,
        )


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
