"""Unix socket-based UI for running the agent as a daemon.

Listens on a Unix domain socket for client connections. Each connected
client can send JSON-delimited messages and receive streamed JSON events
back. Only one client session is active at a time; additional connections
are rejected until the current session ends.

Wire protocol (newline-delimited JSON):

  Client -> Server:
    {"message": "check disk space on gameserver-01"}

  Server -> Client:
    {"type": "tool_call", "tool": "run_remote_command", "input": {...}}
    {"type": "tool_result", "tool": "run_remote_command", "result": {...}}
    {"type": "response", "text": "The disk usage on gameserver-01 is..."}
    {"type": "done"}
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()


class DaemonUI:
    """Non-interactive UI that communicates over a Unix domain socket."""

    def __init__(self, socket_path: str) -> None:
        """Initialize the daemon UI.

        Args:
            socket_path: Filesystem path for the Unix domain socket.
        """
        self._socket_path = socket_path
        self._server: asyncio.AbstractServer | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._client_connected = asyncio.Event()
        self._shutdown = asyncio.Event()

    async def start(self) -> None:
        """Start the Unix socket server."""
        path = Path(self._socket_path)
        if path.exists():
            path.unlink()
        path.parent.mkdir(parents=True, exist_ok=True)

        self._server = await asyncio.start_unix_server(
            self._handle_connection,
            path=self._socket_path,
        )
        # Owner + group read/write, no world access
        os.chmod(self._socket_path, 0o660)
        logger.info("daemon_listening", socket=self._socket_path)

    async def stop(self) -> None:
        """Shut down the socket server and clean up."""
        self._shutdown.set()
        self._client_connected.set()  # Unblock any pending wait_for_client
        self._cleanup_client()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        path = Path(self._socket_path)
        if path.exists():
            path.unlink()
        logger.info("daemon_stopped")

    async def wait_for_client(self) -> bool:
        """Block until a client connects.

        Returns:
            True if a client connected, False if the daemon is shutting down.
        """
        self._client_connected.clear()
        await self._client_connected.wait()
        return not self._shutdown.is_set()

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle an incoming client connection."""
        if self._reader is not None:
            error = json.dumps({"type": "error", "text": "Another session is active"}) + "\n"
            writer.write(error.encode())
            await writer.drain()
            writer.close()
            return

        self._reader = reader
        self._writer = writer
        self._client_connected.set()
        logger.info("client_connected")

    async def get_input(self) -> str | None:
        """Read the next message from the connected client.

        Returns:
            The message string, or None if the client disconnected.
        """
        if self._reader is None:
            return None

        try:
            line = await self._reader.readline()
            if not line:
                self._cleanup_client()
                return None

            data = json.loads(line.decode().strip())
            return data.get("message", "").strip()
        except (json.JSONDecodeError, ConnectionError, OSError) as e:
            logger.warning("client_read_error", error=str(e))
            self._cleanup_client()
            return None

    def _send_event(self, event: dict[str, Any]) -> None:
        """Send a JSON event line to the connected client (non-blocking enqueue)."""
        if self._writer is not None and not self._writer.is_closing():
            try:
                data = json.dumps(event, default=str) + "\n"
                self._writer.write(data.encode())
            except (ConnectionError, OSError):
                self._cleanup_client()

    async def flush(self) -> None:
        """Flush pending writes to the client."""
        if self._writer and not self._writer.is_closing():
            try:
                await self._writer.drain()
            except (ConnectionError, OSError):
                self._cleanup_client()

    def _cleanup_client(self) -> None:
        """Clean up the current client connection."""
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer and not writer.is_closing():
            try:
                writer.close()
            except OSError:
                pass
        logger.info("client_disconnected")

    # -- UI interface methods (matching TerminalUI) --

    def display_banner(self, version: str, model: str, servers: list[str]) -> None:
        """Send startup banner info to the client."""
        self._send_event({
            "type": "banner",
            "version": version,
            "model": model,
            "servers": servers,
        })

    def display_response(self, text: str) -> None:
        """Send Claude's text response to the client."""
        self._send_event({"type": "response", "text": text})

    def display_tool_call(self, tool_name: str, tool_input: dict[str, Any]) -> None:
        """Send a tool call notification to the client."""
        self._send_event({"type": "tool_call", "tool": tool_name, "input": tool_input})

    def display_tool_result(self, tool_name: str, result: dict[str, Any]) -> None:
        """Send a tool result to the client."""
        self._send_event({"type": "tool_result", "tool": tool_name, "result": result})

    def display_error(self, message: str) -> None:
        """Send an error message to the client."""
        self._send_event({"type": "error", "text": message})

    def display_info(self, message: str) -> None:
        """Send an informational message to the client."""
        self._send_event({"type": "info", "text": message})

    def display_done(self) -> None:
        """Send a message-complete marker so the client knows the turn is over."""
        self._send_event({"type": "done"})

    def display_goodbye(self) -> None:
        """Send session-end marker to the client."""
        self._send_event({"type": "goodbye"})
