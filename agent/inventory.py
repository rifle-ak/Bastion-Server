"""Server inventory model and accessor.

Provides a typed interface over the server inventory for looking up
servers by name, role, or service, and retrieving connection details.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.config import PermissionsConfig, RolePermissions, ServerDefinition, ServersConfig


@dataclass(frozen=True)
class ServerInfo:
    """Resolved server information with its permissions attached."""

    name: str
    definition: ServerDefinition
    permissions: RolePermissions


class Inventory:
    """Manages the server inventory and provides lookup methods."""

    def __init__(self, servers_cfg: ServersConfig, permissions_cfg: PermissionsConfig) -> None:
        """Initialize inventory from loaded configs."""
        self._servers = servers_cfg.servers
        self._permissions = permissions_cfg
        self._by_role: dict[str, list[str]] = {}
        for name, server in self._servers.items():
            self._by_role.setdefault(server.role, []).append(name)

    @property
    def server_names(self) -> list[str]:
        """Return all server names in the inventory."""
        return list(self._servers.keys())

    @property
    def roles(self) -> list[str]:
        """Return all distinct roles present in the inventory."""
        return list(self._by_role.keys())

    def get_server(self, name: str) -> ServerInfo:
        """Look up a server by name, returning its definition and permissions.

        Raises:
            KeyError: If the server name is not in the inventory.
        """
        if name not in self._servers:
            raise KeyError(f"Unknown server: {name!r}. Available: {', '.join(self.server_names)}")
        defn = self._servers[name]
        role_perms = self._permissions.roles.get(defn.role, RolePermissions())
        return ServerInfo(name=name, definition=defn, permissions=role_perms)

    def get_servers_by_role(self, role: str) -> list[ServerInfo]:
        """Return all servers matching the given role."""
        names = self._by_role.get(role, [])
        return [self.get_server(n) for n in names]

    def get_approval_patterns(self) -> list[str]:
        """Return the global list of patterns that require human approval."""
        return self._permissions.approval_required_patterns

    def format_for_prompt(self) -> str:
        """Format the inventory for inclusion in the system prompt."""
        lines: list[str] = []
        for name, server in self._servers.items():
            lines.append(f"- **{name}** ({server.role}): {server.description}")
            lines.append(f"  Host: {server.host} | User: {server.user} | SSH: {server.ssh}")
            if server.services:
                lines.append(f"  Services: {', '.join(server.services)}")
            if server.metrics_url:
                lines.append(f"  Metrics: {server.metrics_url}")
        return "\n".join(lines)
