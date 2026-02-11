"""Structured JSON audit logging using structlog.

Every tool call is logged — attempts, successes, denials, errors, and
timeouts — to a JSONL file for post-hoc review and compliance.
"""

from __future__ import annotations

import sys
from pathlib import Path

import structlog


def configure_audit_logger(log_path: str) -> structlog.BoundLogger:
    """Create and configure a dedicated audit logger writing JSON to a file.

    Args:
        log_path: Path to the JSONL audit log file.

    Returns:
        A bound structlog logger for audit events.
    """
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Open the file in append mode for the file logger
    log_file = open(path, "a", buffering=1)  # line-buffered  # noqa: SIM115

    # Build a logger factory that writes to the audit file
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(0),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=log_file),
        cache_logger_on_first_use=False,
    )

    return structlog.get_logger("audit")


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


def _truncate_result(result: dict, max_len: int = 2000) -> dict:
    """Truncate string values in a result dict to prevent log bloat."""
    truncated = {}
    for k, v in result.items():
        if isinstance(v, str) and len(v) > max_len:
            truncated[k] = v[:max_len] + f"... (truncated, {len(v)} total)"
        else:
            truncated[k] = v
    return truncated
