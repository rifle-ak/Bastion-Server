"""Tests for the self_update tool and approval integration."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent.security.approval import ALWAYS_REQUIRE_APPROVAL, requires_approval
from agent.tools.self_update import SelfUpdate


class TestSelfUpdateSchema:
    def setup_method(self):
        self.tool = SelfUpdate()

    def test_name(self):
        assert self.tool.name == "self_update"

    def test_description_mentions_check_and_update(self):
        desc = self.tool.description
        assert "check" in desc.lower()
        assert "update" in desc.lower()

    def test_parameters_has_action(self):
        params = self.tool.parameters
        assert "action" in params["properties"]
        assert params["properties"]["action"]["enum"] == ["check", "update"]
        assert "action" in params["required"]

    def test_parameters_has_branch(self):
        params = self.tool.parameters
        assert "branch" in params["properties"]
        # branch is optional
        assert "branch" not in params["required"]

    def test_schema_generation(self):
        schema = self.tool.to_schema()
        assert schema["name"] == "self_update"
        assert "input_schema" in schema


class TestSelfUpdateApproval:
    """Verify that self_update integrates correctly with the approval system."""

    def test_self_update_in_always_require_approval(self):
        assert "self_update" in ALWAYS_REQUIRE_APPROVAL

    def test_check_action_does_not_require_approval(self):
        result = requires_approval(
            "self_update",
            {"action": "check"},
            [],  # no patterns needed, it's in ALWAYS_REQUIRE_APPROVAL
        )
        assert result is False

    def test_update_action_requires_approval(self):
        result = requires_approval(
            "self_update",
            {"action": "update"},
            [],
        )
        assert result is True

    def test_update_action_requires_approval_with_branch(self):
        result = requires_approval(
            "self_update",
            {"action": "update", "branch": "dev"},
            [],
        )
        assert result is True


class TestSelfUpdateCheck:
    """Test the check action (read-only)."""

    def setup_method(self):
        self.tool = SelfUpdate()

    @pytest.mark.asyncio
    async def test_check_returns_version_info(self):
        """Check action should return version info without making changes."""
        with patch("agent.tools.self_update._INSTALL_DIR", "/nonexistent"):
            result = await self.tool.execute(action="check")
            assert "Current version" in result.output
            assert "Install directory" in result.output
            # Should note missing install dir
            assert "not found" in result.output

    @pytest.mark.asyncio
    async def test_check_with_branch(self):
        result = await self.tool.execute(action="check", branch="dev")
        assert "Branch: dev" in result.output

    @pytest.mark.asyncio
    async def test_check_default_branch(self):
        result = await self.tool.execute(action="check")
        assert "Branch: main" in result.output

    @pytest.mark.asyncio
    async def test_unknown_action(self):
        result = await self.tool.execute(action="bogus")
        assert result.exit_code == 1
        assert "Unknown action" in result.error


class TestSelfUpdateUpdate:
    """Test the update action (with mocks — no real downloads)."""

    def setup_method(self):
        self.tool = SelfUpdate()

    @pytest.mark.asyncio
    async def test_update_fails_if_install_dir_missing(self):
        with patch("agent.tools.self_update._INSTALL_DIR", "/nonexistent"):
            result = await self.tool.execute(action="update")
            assert result.exit_code == 1
            assert "not found" in result.error


class TestApprovalAlwaysRequire:
    """Test the ALWAYS_REQUIRE_APPROVAL mechanism more broadly."""

    def test_callable_checker_true(self):
        """Callable returns True -> requires approval."""
        result = requires_approval(
            "self_update",
            {"action": "update"},
            [],
        )
        assert result is True

    def test_callable_checker_false(self):
        """Callable returns False -> does not require approval."""
        result = requires_approval(
            "self_update",
            {"action": "check"},
            [],
        )
        assert result is False

    def test_other_tools_unaffected(self):
        """Other tools should not be affected by ALWAYS_REQUIRE_APPROVAL."""
        result = requires_approval(
            "list_servers",
            {"action": "update"},  # even with "update" in input
            [],
        )
        assert result is False

    def test_pattern_based_approval_still_works(self):
        """Regular pattern-based approval should still work for other tools."""
        result = requires_approval(
            "run_remote_command",
            {"command": "docker restart nginx"},
            ["restart"],
        )
        assert result is True
