"""Unix socket-based UI for running the agent as a daemon.

Listens on a Unix domain socket for client connections. Each connected
client can send JSON-delimited messages and receive streamed JSON events
back. Only one client session is active at a time; additional connections
are rejected until the current session ends.

Wire protocol (newline-delimited JSON):

  Client -> Server:
    {"message": "check disk space on gameserver-01"}
    {"type": "cancel"}

  Server -> Client:
    {"type": "tool_call", "tool": "run_remote_command", "input": {...}}
    {"type": "tool_result", "tool": "run_remote_command", "result": {...}}
    {"type": "response", "text": "The disk usage on gameserver-01 is..."}
    {"type": "cancelled"}
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
        self._cancelled = asyncio.Event()
        self._monitor_task: asyncio.Task[None] | None = None
        self._last_metadata: dict[str, Any] = {}

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
            # Check if the existing client is actually still alive.
            # A stale session can happen if the old client disconnected
            # while the daemon was blocked (e.g. in a sync API call
            # before the run_in_executor + monitor changes).
            stale = False
            if self._writer is None or self._writer.is_closing():
                stale = True
            elif self._reader.at_eof():
                stale = True
            else:
                # Try a zero-byte probe write on the old writer.
                # If the transport is dead, this sets an internal error
                # state that we can detect via is_closing().
                try:
                    self._writer.write(b"")
                    await self._writer.drain()
                    if self._writer.is_closing():
                        stale = True
                except (ConnectionError, OSError):
                    stale = True

            if stale:
                logger.info("stale_session_detected", msg="cleaning up dead client")
                self._cleanup_client()
            else:
                # Genuinely active — reject with a clean terminal event
                # so the new client's _read_events() exits normally.
                error = json.dumps({"type": "error", "text": "Another session is active. Run: bastion restart"}) + "\n"
                done = json.dumps({"type": "done"}) + "\n"
                writer.write(error.encode())
                writer.write(done.encode())
                await writer.drain()
                writer.close()
                return

        self._reader = reader
        self._writer = writer
        self._client_connected.set()
        logger.info("client_connected")

    @property
    def cancelled_event(self) -> asyncio.Event:
        """The cancellation event — set when the client disconnects or sends cancel."""
        return self._cancelled

    @property
    def is_cancelled(self) -> bool:
        """True if the current operation was cancelled by the client."""
        return self._cancelled.is_set()

    def start_processing(self) -> None:
        """Start monitoring the client socket for disconnect or cancel.

        Call before starting a long-running operation (API call / tool
        execution).  While the daemon processes a message it does NOT
        call ``get_input()``, so this background task watches the reader
        for client disconnect (EOF) or an explicit cancel message.
        """
        self._cancelled.clear()
        if self._reader is not None:
            self._monitor_task = asyncio.create_task(self._monitor_client())

    def stop_processing(self) -> None:
        """Stop the client-disconnect monitor."""
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        self._monitor_task = None

    async def _monitor_client(self) -> None:
        """Background task: watch for client disconnect or cancel message."""
        if self._reader is None:
            return
        try:
            while not self._cancelled.is_set():
                line = await self._reader.readline()
                if not line:
                    # EOF — client disconnected
                    logger.info("client_disconnected_during_processing")
                    self._cancelled.set()
                    return
                try:
                    data = json.loads(line.decode().strip())
                    if data.get("type") == "cancel":
                        logger.info("cancel_requested_by_client")
                        self._cancelled.set()
                        return
                except json.JSONDecodeError:
                    continue
        except (ConnectionError, OSError):
            self._cancelled.set()
        except asyncio.CancelledError:
            pass  # Normal cleanup from stop_processing()

    @property
    def last_metadata(self) -> dict[str, Any]:
        """Return metadata from the last parsed client message.

        This includes any extra fields beyond ``message``, such as
        ``resume`` for session resumption.
        """
        return dict(self._last_metadata)

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
            self._last_metadata = data
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
        self._cancelled.set()  # Unblock anything waiting on cancel
        self.stop_processing()
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

    def display_cancelled(self) -> None:
        """Send a cancellation acknowledgement to the client."""
        self._send_event({"type": "cancelled", "text": "Operation cancelled."})

    def display_done(self) -> None:
        """Send a message-complete marker so the client knows the turn is over."""
        self._send_event({"type": "done"})

    def display_goodbye(self) -> None:
        """Send session-end marker to the client."""
        self._send_event({"type": "goodbye"})
