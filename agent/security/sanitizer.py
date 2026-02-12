"""Input sanitization to prevent shell injection.

Rejects inputs containing dangerous shell metacharacters rather than
attempting to escape them. Commands that need pipes or chaining must
be built programmatically in tool implementations.
"""

from __future__ import annotations

import re

import structlog

logger = structlog.get_logger()

# These patterns are REJECTED outright — never escaped
FORBIDDEN_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r'[;&|]'),            # Command chaining
    re.compile(r'\$[\({]'),          # Command/variable substitution $() and ${}
    re.compile(r'`'),                # Backtick substitution
    re.compile(r'\.\.'),             # Path traversal
    re.compile(r'>\s*/'),            # Redirect to absolute path
    re.compile(r'>>\s*/'),           # Append to absolute path
    re.compile(r'\b(eval|exec)\b'),  # Code execution keywords
    re.compile(r'[\n\r\x00]'),       # Newline/carriage-return/null-byte injection
]

# Human-readable reason for each pattern (same order)
_PATTERN_REASONS: list[str] = [
    "command chaining characters (;, &, |)",
    "command/variable substitution ($( or ${)",
    "backtick substitution",
    "path traversal (..)",
    "redirect to absolute path",
    "append to absolute path",
    "eval/exec keyword",
    "newline/null-byte injection",
]


class SanitizationError(Exception):
    """Raised when input fails sanitization checks."""

    def __init__(self, field: str, value: str, reason: str) -> None:
        self.field = field
        self.value = value
        self.reason = reason
        super().__init__(f"Rejected {field}: {reason}")


def check_command(command: str) -> None:
    """Validate a command string against forbidden patterns.

    Args:
        command: The command string to validate.

    Raises:
        SanitizationError: If the command contains forbidden patterns.
    """
    for pattern, reason in zip(FORBIDDEN_PATTERNS, _PATTERN_REASONS):
        if pattern.search(command):
            logger.warning("sanitizer_rejected", command=command, reason=reason)
            raise SanitizationError("command", command, reason)


def check_path(path: str) -> None:
    """Validate a file path against forbidden patterns.

    Args:
        path: The file path to validate.

    Raises:
        SanitizationError: If the path contains forbidden patterns.
    """
    if re.search(r'\.\.', path):
        raise SanitizationError("path", path, "path traversal (..)")
    if re.search(r'[;&|`]', path):
        raise SanitizationError("path", path, "shell metacharacters in path")
    if re.search(r'\$[\({]', path):
        raise SanitizationError("path", path, "command/variable substitution in path")
    if re.search(r'[\n\r\x00]', path):
        raise SanitizationError("path", path, "newline/null-byte in path")


def sanitize(tool_name: str, tool_input: dict) -> dict:
    """Sanitize all inputs for a tool call.

    Checks 'command' and 'path' fields if present. Returns the input
    unchanged if everything passes — we reject bad input, not modify it.

    Args:
        tool_name: Name of the tool being called.
        tool_input: The tool's input parameters.

    Returns:
        The original tool_input dict if all checks pass.

    Raises:
        SanitizationError: If any input field fails validation.
    """
    if "command" in tool_input:
        check_command(tool_input["command"])

    if "path" in tool_input:
        check_path(tool_input["path"])

    # Validate container names, service names, etc. — no shell chars
    for field in ("container", "service", "server"):
        if field in tool_input:
            value = tool_input[field]
            if re.search(r'[;&|`$]', value):
                raise SanitizationError(field, value, "shell metacharacters")

    return tool_input
