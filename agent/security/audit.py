"""Structured JSON audit logging using structlog.

Every tool call is logged — attempts, successes, denials, errors, and
timeouts — to a JSONL file for post-hoc review and compliance.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog


class AuditLogger:
    """Structured audit logger for tool execution events."""

    def __init__(self, log_path: str) -> None:
        """Initialize the audit logger.

        Args:
            log_path: Path to the JSONL audit log file.
        """
        self._log_path = Path(log_path)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self._log_path, "a", buffering=1)  # noqa: SIM115

        # Create a dedicated structlog logger writing JSON to the audit file
        self._logger = structlog.wrap_logger(
            structlog.PrintLogger(file=self._file),
            processors=[
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.JSONRenderer(),
            ],
        )

    def log_session_start(self) -> None:
        """Log that a new session has started (interactive or daemon)."""
        import getpass
        import os

        try:
            tty = os.ttyname(0) if os.isatty(0) else "none"
        except OSError:
            tty = "none"

        self._logger.info(
            "session_start",
            user=getpass.getuser(),
            pid=os.getpid(),
            tty=tty,
        )

    def log_session_end(self) -> None:
        """Log that a session has ended."""
        self._logger.info("session_end")

    def log_attempt(self, tool_name: str, tool_input: dict) -> None:
        """Log that a tool call is being attempted."""
        self._logger.info("tool_attempt", tool=tool_name, input=tool_input)

    def log_success(self, tool_name: str, tool_input: dict, result: dict) -> None:
        """Log a successful tool execution."""
        # Truncate large results to avoid bloating the log
        truncated = _truncate_result(result)
        self._logger.info(
            "tool_success", tool=tool_name, input=tool_input, result=truncated
        )

    def log_denied(self, tool_name: str, tool_input: dict, reason: str) -> None:
        """Log a denied tool call (allowlist or human denial)."""
        self._logger.warning(
            "tool_denied", tool=tool_name, input=tool_input, reason=reason
        )

    def log_error(self, tool_name: str, tool_input: dict, error: str) -> None:
        """Log a tool execution error."""
        self._logger.error(
            "tool_error", tool=tool_name, input=tool_input, error=error
        )

    def log_timeout(self, tool_name: str, tool_input: dict) -> None:
        """Log a tool execution timeout."""
        self._logger.warning(
            "tool_timeout", tool=tool_name, input=tool_input
        )

    def close(self) -> None:
        """Close the audit log file."""
        self._file.close()

    def __enter__(self) -> AuditLogger:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def _truncate_result(result: dict, max_len: int = 2000) -> dict:
    """Truncate string values in a result dict to prevent log bloat."""
    truncated = {}
    for k, v in result.items():
        if isinstance(v, str) and len(v) > max_len:
            truncated[k] = v[:max_len] + f"... (truncated, {len(v)} total)"
        else:
            truncated[k] = v
    return truncated
