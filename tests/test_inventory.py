"""Tests for inventory loading and lookup."""

from __future__ import annotations

import pytest

from agent.config import (
    PermissionsConfig,
    RolePermissions,
    ServerDefinition,
    ServersConfig,
)
from agent.inventory import Inventory, ServerInfo


@pytest.fixture
def servers_cfg() -> ServersConfig:
    return ServersConfig(
        servers={
            "localhost": ServerDefinition(
                host="localhost",
                role="bastion",
                user="claude-agent",
                description="Bastion server",
                ssh=False,
            ),
            "web-01": ServerDefinition(
                host="10.0.1.10",
                role="web",
                user="claude-agent",
                description="Web server",
                key_path="/tmp/test_key",
                services=["nginx", "php-fpm"],
            ),
            "web-02": ServerDefinition(
                host="10.0.1.11",
                role="web",
                user="claude-agent",
                description="Web server 2",
                key_path="/tmp/test_key2",
            ),
        }
    )


@pytest.fixture
def permissions_cfg() -> PermissionsConfig:
    return PermissionsConfig(
        roles={
            "bastion": RolePermissions(
                allowed_commands=["uptime", "df -h"],
                allowed_paths_read=["/var/log/"],
            ),
            "web": RolePermissions(
                allowed_commands=["uptime", "systemctl status *"],
                allowed_paths_read=["/var/log/", "/etc/nginx/"],
            ),
        },
        approval_required_patterns=["restart", "stop"],
    )


@pytest.fixture
def inventory(servers_cfg, permissions_cfg) -> Inventory:
    return Inventory(servers_cfg, permissions_cfg)


class TestInventoryLookup:
    """Tests for server lookup."""

    def test_get_server_exists(self, inventory):
        info = inventory.get_server("localhost")
        assert isinstance(info, ServerInfo)
        assert info.name == "localhost"
        assert info.definition.role == "bastion"

    def test_get_server_not_found(self, inventory):
        with pytest.raises(KeyError, match="Unknown server"):
            inventory.get_server("nonexistent")

    def test_server_names(self, inventory):
        names = inventory.server_names
        assert set(names) == {"localhost", "web-01", "web-02"}

    def test_roles(self, inventory):
        roles = inventory.roles
        assert set(roles) == {"bastion", "web"}


class TestInventoryPermissions:
    """Tests for permission resolution."""

    def test_permissions_resolved(self, inventory):
        info = inventory.get_server("localhost")
        assert "uptime" in info.permissions.allowed_commands

    def test_web_permissions(self, inventory):
        info = inventory.get_server("web-01")
        assert "systemctl status *" in info.permissions.allowed_commands

    def test_missing_role_gets_empty_perms(self):
        """A server with a role not in permissions gets empty permissions."""
        servers = ServersConfig(
            servers={"test": ServerDefinition(host="1.2.3.4", role="unknown")}
        )
        perms = PermissionsConfig(roles={})
        inv = Inventory(servers, perms)
        info = inv.get_server("test")
        assert info.permissions.allowed_commands == []


class TestInventoryByRole:
    """Tests for role-based lookups."""

    def test_get_by_role(self, inventory):
        web_servers = inventory.get_servers_by_role("web")
        assert len(web_servers) == 2
        names = {s.name for s in web_servers}
        assert names == {"web-01", "web-02"}

    def test_get_by_role_empty(self, inventory):
        assert inventory.get_servers_by_role("database") == []

    def test_approval_patterns(self, inventory):
        patterns = inventory.get_approval_patterns()
        assert "restart" in patterns
        assert "stop" in patterns


class TestFormatForPrompt:
    """Tests for prompt formatting."""

    def test_format_includes_all_servers(self, inventory):
        output = inventory.format_for_prompt()
        assert "localhost" in output
        assert "web-01" in output
        assert "web-02" in output

    def test_format_includes_services(self, inventory):
        output = inventory.format_for_prompt()
        assert "nginx" in output
        assert "php-fpm" in output
