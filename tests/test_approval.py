"""Tests for the approval gate."""

from __future__ import annotations

import pytest

from agent.security.approval import requires_approval


@pytest.fixture
def approval_patterns() -> list[str]:
    return [
        "restart", "stop", "kill", "rm ", "remove", "delete",
        "drop", "truncate", "write", "tee ", ">", ">>",
    ]


class TestRequiresApproval:
    """Tests for approval requirement detection."""

    def test_safe_tools_never_need_approval(self, approval_patterns):
        """list_servers and query_metrics are always safe."""
        assert requires_approval("list_servers", {}, approval_patterns) is False
        assert requires_approval("query_metrics", {"query": "up"}, approval_patterns) is False

    def test_restart_command_needs_approval(self, approval_patterns):
        assert requires_approval(
            "run_remote", {"command": "docker restart foo"}, approval_patterns
        ) is True

    def test_stop_needs_approval(self, approval_patterns):
        assert requires_approval(
            "run_remote", {"command": "systemctl stop nginx"}, approval_patterns
        ) is True

    def test_kill_needs_approval(self, approval_patterns):
        assert requires_approval(
            "run_local", {"command": "kill -9 1234"}, approval_patterns
        ) is True

    def test_rm_needs_approval(self, approval_patterns):
        assert requires_approval(
            "run_local", {"command": "rm /tmp/file"}, approval_patterns
        ) is True

    def test_read_only_no_approval(self, approval_patterns):
        assert requires_approval(
            "run_local", {"command": "uptime"}, approval_patterns
        ) is False

    def test_docker_ps_no_approval(self, approval_patterns):
        assert requires_approval(
            "docker_ps", {"server": "localhost"}, approval_patterns
        ) is False

    def test_df_no_approval(self, approval_patterns):
        assert requires_approval(
            "run_local", {"command": "df -h"}, approval_patterns
        ) is False

    def test_case_insensitive_matching(self, approval_patterns):
        """Approval patterns should match case-insensitively."""
        assert requires_approval(
            "run_local", {"command": "RESTART service"}, approval_patterns
        ) is True

    def test_service_field_checked(self, approval_patterns):
        """Non-command string fields should also be checked."""
        assert requires_approval(
            "service_status", {"server": "host", "service": "restart-helper"}, approval_patterns
        ) is True
