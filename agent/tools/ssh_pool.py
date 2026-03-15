"""SSH connection pool for session-level reuse.

Instead of opening a fresh SSH connection for every tool call
(1-2s handshake overhead each time), the pool maintains open
connections keyed by server name. Connections are reused across
tool calls within a conversation session.

Usage::

    pool = SSHPool()
    result = await pool.run(server_info, "uptime")
    result2 = await pool.run(server_info, "df -h")  # reuses connection
    await pool.close_all()
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import structlog

from agent.inventory import ServerInfo
from agent.tools.base import ToolResult

logger = structlog.get_logger()

_CONNECT_TIMEOUT = 10


class SSHPool:
    """Maintains a pool of SSH connections keyed by server name.

    Thread-safe via asyncio locks. Connections are lazily opened on
    first use and reused for subsequent commands to the same server.
    """

    def __init__(self) -> None:
        self._connections: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    async def _get_lock(self, server_name: str) -> asyncio.Lock:
        """Get or create a per-server lock."""
        async with self._global_lock:
            if server_name not in self._locks:
                self._locks[server_name] = asyncio.Lock()
            return self._locks[server_name]

    async def _get_connection(self, server_info: ServerInfo) -> Any:
        """Get or create an SSH connection for a server."""
        try:
            import asyncssh
        except ImportError:
            raise RuntimeError("asyncssh not available")

        name = server_info.name
        lock = await self._get_lock(name)

        async with lock:
            # Check if existing connection is still alive
            conn = self._connections.get(name)
            if conn is not None:
                try:
                    # Quick check — if the transport is closed, discard
                    if not conn._transport or conn._transport.is_closing():
                        logger.debug("ssh_pool_stale", server=name)
                        conn = None
                        del self._connections[name]
                    else:
                        return conn
                except Exception:
                    conn = None
                    self._connections.pop(name, None)

            # Open new connection
            defn = server_info.definition

            if not defn.key_path:
                raise RuntimeError(f"No SSH key configured for {name}")

            key_file = Path(defn.key_path)
            if not key_file.exists():
                raise RuntimeError(f"SSH key not found: {defn.key_path}")

            # Retry with exponential backoff for transient failures
            last_err: Exception | None = None
            for attempt in range(3):
                try:
                    logger.debug(
                        "ssh_pool_connect",
                        server=name, host=defn.host, attempt=attempt + 1,
                    )
                    conn = await asyncio.wait_for(
                        asyncssh.connect(
                            defn.host,
                            username=defn.user,
                            client_keys=[defn.key_path],
                            known_hosts=defn.known_hosts_path,
                            keepalive_interval=30,
                        ),
                        timeout=_CONNECT_TIMEOUT,
                    )
                    self._connections[name] = conn
                    return conn
                except (asyncio.TimeoutError, OSError) as e:
                    last_err = e
                    if attempt < 2:
                        delay = 2 ** attempt  # 1s, 2s
                        logger.debug(
                            "ssh_pool_retry",
                            server=name, delay=delay, error=str(e),
                        )
                        await asyncio.sleep(delay)
            raise last_err or RuntimeError(f"SSH connect failed for {name}")

    async def run(
        self,
        server_info: ServerInfo,
        command: str,
        timeout: int = 30,
    ) -> ToolResult:
        """Run a command on a remote server using a pooled connection.

        Args:
            server_info: Server to run on.
            command: Command string to execute.
            timeout: Execution timeout in seconds.

        Returns:
            ToolResult with output.
        """
        name = server_info.name

        if not server_info.definition.ssh:
            return ToolResult(
                error=f"Server {name!r} is local, not SSH.",
                exit_code=1,
            )

        try:
            conn = await self._get_connection(server_info)
        except asyncio.TimeoutError:
            return ToolResult(
                error=f"SSH connect timeout to {name} ({server_info.definition.host})",
                exit_code=1,
            )
        except Exception as e:
            return ToolResult(error=f"SSH connect failed for {name}: {e}", exit_code=1)

        try:
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
            return ToolResult(error=f"Command timed out after {timeout}s on {name}", exit_code=1)
        except Exception as e:
            # Connection might have died — remove from pool
            self._connections.pop(name, None)
            return ToolResult(error=f"SSH command failed on {name}: {e}", exit_code=1)

    async def run_many(
        self,
        server_info: ServerInfo,
        commands: dict[str, str],
        timeout: int = 15,
    ) -> dict[str, ToolResult]:
        """Run multiple commands on one server in parallel over one connection.

        Args:
            server_info: Server to run on.
            commands: Dict of label -> command string.
            timeout: Per-command timeout.

        Returns:
            Dict of label -> ToolResult.
        """
        if not server_info.definition.ssh:
            return {k: ToolResult(error="Local server", exit_code=1) for k in commands}

        try:
            conn = await self._get_connection(server_info)
        except Exception as e:
            return {k: ToolResult(error=f"SSH failed: {e}", exit_code=1) for k in commands}

        async def _run_one(label: str, cmd: str) -> tuple[str, ToolResult]:
            try:
                result = await asyncio.wait_for(
                    conn.run(cmd, check=False), timeout=timeout,
                )
                return label, ToolResult(
                    output=(result.stdout or "").rstrip(),
                    error=(result.stderr or "").rstrip(),
                    exit_code=result.exit_status or 0,
                )
            except asyncio.TimeoutError:
                return label, ToolResult(error=f"Timed out ({timeout}s)", exit_code=1)
            except Exception as e:
                return label, ToolResult(error=str(e), exit_code=1)

        pairs = await asyncio.gather(
            *[_run_one(lbl, cmd) for lbl, cmd in commands.items()]
        )
        return dict(pairs)

    async def close_all(self) -> None:
        """Close all pooled connections."""
        for name, conn in list(self._connections.items()):
            try:
                conn.close()
                await conn.wait_closed()
            except Exception:
                pass
        self._connections.clear()
        logger.debug("ssh_pool_closed")

    @property
    def active_connections(self) -> list[str]:
        """List of server names with active connections."""
        return list(self._connections.keys())
