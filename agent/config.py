"""Configuration loading and validation using Pydantic models.

Loads agent configuration from YAML files and environment variables.
All config models use Pydantic v2 for strict validation.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator


class ApprovalMode(str, Enum):
    """How the agent handles destructive operation approvals."""

    INTERACTIVE = "interactive"
    AUTO_DENY = "auto_deny"


class AgentConfig(BaseModel):
    """Top-level agent behavior configuration loaded from agent.yaml."""

    model: str = "claude-sonnet-4-5-20250929"
    max_tokens: int = Field(default=4096, ge=1, le=8192)
    max_tool_iterations: int = Field(default=10, ge=1, le=50)
    command_timeout: int = Field(default=30, ge=1, le=300)
    audit_log_path: str = "./logs/audit.jsonl"
    approval_mode: ApprovalMode = ApprovalMode.INTERACTIVE
    socket_path: str = "/run/bastion-agent/agent.sock"


class RolePermissions(BaseModel):
    """Permissions for a server role: allowed commands and file paths."""

    allowed_commands: list[str] = Field(default_factory=list)
    allowed_paths_read: list[str] = Field(default_factory=list)
    allowed_paths_write: list[str] = Field(default_factory=list)


class PermissionsConfig(BaseModel):
    """Full permissions configuration loaded from permissions.yaml."""

    roles: dict[str, RolePermissions] = Field(default_factory=dict)
    approval_required_patterns: list[str] = Field(default_factory=list)


class ServerDefinition(BaseModel):
    """Definition of a single server in the inventory."""

    host: str
    role: str
    user: str = "claude-agent"
    description: str = ""
    ssh: bool = True
    key_path: str | None = None
    known_hosts_path: str | None = None
    services: list[str] = Field(default_factory=list)
    metrics_url: str | None = None
    metrics_auth: str | None = Field(
        default=None,
        description="Basic auth credentials for metrics URL (user:password). "
        "If the value starts with '$', it is read from that environment variable.",
    )

    @field_validator("key_path")
    @classmethod
    def expand_key_path(cls, v: str | None) -> str | None:
        """Expand ~ in key paths to the actual home directory."""
        if v is not None:
            return str(Path(v).expanduser())
        return v


class ServersConfig(BaseModel):
    """Server inventory loaded from servers.yaml."""

    servers: dict[str, ServerDefinition] = Field(default_factory=dict)


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load and parse a YAML file, returning an empty dict if missing."""
    if not path.exists():
        return {}
    with open(path) as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def load_agent_config(config_dir: Path) -> AgentConfig:
    """Load agent configuration from config_dir/agent.yaml."""
    data = _load_yaml(config_dir / "agent.yaml")
    return AgentConfig(**data)


def load_permissions_config(config_dir: Path) -> PermissionsConfig:
    """Load permissions from config_dir/permissions.yaml."""
    data = _load_yaml(config_dir / "permissions.yaml")
    return PermissionsConfig(**data)


def load_servers_config(config_dir: Path) -> ServersConfig:
    """Load server inventory from config_dir/servers.yaml."""
    data = _load_yaml(config_dir / "servers.yaml")
    return ServersConfig(**data)


def load_all_config(config_dir: str | Path) -> tuple[AgentConfig, ServersConfig, PermissionsConfig]:
    """Load all configuration files from the given directory.

    Args:
        config_dir: Path to the configuration directory.

    Returns:
        Tuple of (AgentConfig, ServersConfig, PermissionsConfig).

    Raises:
        FileNotFoundError: If config_dir does not exist.
        pydantic.ValidationError: If any config file has invalid content.
    """
    config_path = Path(config_dir)
    if not config_path.is_dir():
        raise FileNotFoundError(f"Configuration directory not found: {config_path}")

    agent_cfg = load_agent_config(config_path)
    servers_cfg = load_servers_config(config_path)
    permissions_cfg = load_permissions_config(config_path)

    return agent_cfg, servers_cfg, permissions_cfg
