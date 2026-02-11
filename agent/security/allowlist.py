"""Command allowlist engine.

Validates commands against glob-style patterns defined per server role
in permissions.yaml. Uses allowlisting — only explicitly permitted
commands can execute.
"""

from __future__ import annotations

import fnmatch
import os

import structlog

from agent.config import RolePermissions

logger = structlog.get_logger()


class AllowlistDenied(Exception):
    """Raised when a command is not on the allowlist."""

    def __init__(self, command: str, role: str) -> None:
        self.command = command
        self.role = role
        super().__init__(f"Command not allowed for role {role!r}: {command!r}")


def is_command_permitted(command: str, permissions: RolePermissions) -> bool:
    """Check if a command matches any allowed pattern for the role.

    Patterns use glob-style matching where * matches any sequence of
    characters. The entire command must match the pattern.

    NOTE: This depends on the sanitizer running first to reject shell
    metacharacters. As defense-in-depth, we also reject them here.

    Args:
        command: The full command string to check.
        permissions: The role's permission set.

    Returns:
        True if the command matches at least one allowed pattern.
    """
    command_stripped = command.strip()

    # Defense-in-depth: reject dangerous chars even if sanitizer missed them
    if any(c in command_stripped for c in ';|&`\n\r\x00'):
        return False

    for pattern in permissions.allowed_commands:
        if fnmatch.fnmatch(command_stripped, pattern):
            return True
    return False


def is_path_readable(path: str, permissions: RolePermissions) -> bool:
    """Check if a file path falls under any allowed read path.

    Args:
        path: The absolute file path to check.
        permissions: The role's permission set.

    Returns:
        True if the path starts with an allowed read directory.
    """
    normalized = _normalize_path(path)
    for allowed in permissions.allowed_paths_read:
        allowed_norm = allowed.rstrip("/") + "/"
        if normalized.startswith(allowed_norm) or normalized == allowed_norm.rstrip("/"):
            return True
    return False


def is_path_writable(path: str, permissions: RolePermissions) -> bool:
    """Check if a file path falls under any allowed write path.

    Args:
        path: The absolute file path to check.
        permissions: The role's permission set.

    Returns:
        True if the path starts with an allowed write directory.
    """
    normalized = _normalize_path(path)
    for allowed in permissions.allowed_paths_write:
        allowed_norm = allowed.rstrip("/") + "/"
        if normalized.startswith(allowed_norm) or normalized == allowed_norm.rstrip("/"):
            return True
    return False


def check_command(command: str, role: str, permissions: RolePermissions) -> None:
    """Validate a command against the allowlist, raising on denial.

    Args:
        command: The command to validate.
        role: The server role name (for error messages).
        permissions: The role's permission set.

    Raises:
        AllowlistDenied: If the command is not permitted.
    """
    if not is_command_permitted(command, permissions):
        logger.warning("allowlist_denied", command=command, role=role)
        raise AllowlistDenied(command, role)


def check_path_read(path: str, role: str, permissions: RolePermissions) -> None:
    """Validate a read path against the allowlist, raising on denial.

    Args:
        path: The file path to validate.
        role: The server role name (for error messages).
        permissions: The role's permission set.

    Raises:
        AllowlistDenied: If the path is not readable.
    """
    if not is_path_readable(path, permissions):
        logger.warning("path_read_denied", path=path, role=role)
        raise AllowlistDenied(f"read:{path}", role)


def _normalize_path(path: str) -> str:
    """Normalize a path for comparison.

    Uses os.path.normpath to handle redundant slashes and '.' components.
    We don't resolve symlinks. The sanitizer rejects '..' before we get here.
    """
    return os.path.normpath(path)
