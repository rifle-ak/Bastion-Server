"""Tests for the command allowlist engine."""

from __future__ import annotations

import pytest

from agent.config import RolePermissions
from agent.security.allowlist import (
    AllowlistDenied,
    check_command,
    is_command_permitted,
    is_path_readable,
    is_path_writable,
)


@pytest.fixture
def bastion_perms() -> RolePermissions:
    return RolePermissions(
        allowed_commands=[
            "uptime",
            "df -h",
            "free -h",
            "ps aux",
            "systemctl status *",
            "journalctl -u * --no-pager -n *",
            "docker ps",
            "docker logs *",
            "cat /var/log/*",
            "tail -n * /var/log/*",
        ],
        allowed_paths_read=["/var/log/", "/etc/", "/home/claude-agent/"],
        allowed_paths_write=[],
    )


@pytest.fixture
def game_perms() -> RolePermissions:
    return RolePermissions(
        allowed_commands=[
            "uptime",
            "docker ps *",
            "docker restart *",
            "systemctl restart *",
        ],
        allowed_paths_read=["/var/log/", "/etc/pterodactyl/"],
        allowed_paths_write=[],
    )


class TestIsCommandPermitted:
    """Tests for command pattern matching."""

    def test_exact_match(self, bastion_perms):
        assert is_command_permitted("uptime", bastion_perms) is True

    def test_exact_match_with_args(self, bastion_perms):
        assert is_command_permitted("df -h", bastion_perms) is True

    def test_wildcard_match(self, bastion_perms):
        assert is_command_permitted("systemctl status nginx", bastion_perms) is True

    def test_wildcard_match_multiple(self, bastion_perms):
        assert is_command_permitted("journalctl -u nginx --no-pager -n 100", bastion_perms) is True

    def test_docker_logs_wildcard(self, bastion_perms):
        assert is_command_permitted("docker logs my-container", bastion_perms) is True

    def test_not_permitted(self, bastion_perms):
        assert is_command_permitted("rm -rf /", bastion_perms) is False

    def test_partial_match_rejected(self, bastion_perms):
        """'docker' alone shouldn't match 'docker ps'."""
        assert is_command_permitted("docker", bastion_perms) is False

    def test_docker_ps_with_args(self, game_perms):
        assert is_command_permitted("docker ps -a", game_perms) is True

    def test_docker_restart(self, game_perms):
        assert is_command_permitted("docker restart mycontainer", game_perms) is True

    def test_whitespace_stripped(self, bastion_perms):
        assert is_command_permitted("  uptime  ", bastion_perms) is True


class TestCheckCommand:
    """Tests for check_command which raises on denial."""

    def test_permitted_passes(self, bastion_perms):
        check_command("uptime", "bastion", bastion_perms)  # Should not raise

    def test_denied_raises(self, bastion_perms):
        with pytest.raises(AllowlistDenied, match="bastion"):
            check_command("whoami", "bastion", bastion_perms)


class TestPathChecks:
    """Tests for path allowlist checks."""

    def test_read_allowed(self, bastion_perms):
        assert is_path_readable("/var/log/syslog", bastion_perms) is True

    def test_read_allowed_etc(self, bastion_perms):
        assert is_path_readable("/etc/hostname", bastion_perms) is True

    def test_read_denied(self, bastion_perms):
        assert is_path_readable("/root/.bashrc", bastion_perms) is False

    def test_read_denied_home(self, bastion_perms):
        assert is_path_readable("/home/other-user/file", bastion_perms) is False

    def test_write_denied_empty(self, bastion_perms):
        """Bastion has no write paths, everything should be denied."""
        assert is_path_writable("/var/log/test", bastion_perms) is False

    def test_read_subdirectory(self, game_perms):
        assert is_path_readable("/etc/pterodactyl/config.yml", game_perms) is True

    def test_read_outside_allowed(self, game_perms):
        assert is_path_readable("/etc/shadow", game_perms) is False
