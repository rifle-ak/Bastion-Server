"""Tests for the human-in-the-loop approval gate."""

from __future__ import annotations

import pytest

from agent.security.approval import (
    ALWAYS_SAFE_TOOLS,
    _extract_string_values,
    requires_approval,
)

# Standard approval patterns from the spec
APPROVAL_PATTERNS = [
    "restart",
    "stop",
    "kill",
    "rm ",
    "remove",
    "delete",
    "drop",
    "truncate",
    "write",
    "tee ",
    ">",
    ">>",
]


# --- requires_approval ---


class TestRequiresApproval:
    """Tests for approval requirement detection."""

    def test_safe_command_no_approval(self) -> None:
        result = requires_approval(
            "run_local_command",
            {"command": "uptime"},
            APPROVAL_PATTERNS,
        )
        assert result is False

    def test_restart_requires_approval(self) -> None:
        result = requires_approval(
            "run_local_command",
            {"command": "docker restart my-app"},
            APPROVAL_PATTERNS,
        )
        assert result is True

    def test_stop_requires_approval(self) -> None:
        result = requires_approval(
            "run_local_command",
            {"command": "systemctl stop nginx"},
            APPROVAL_PATTERNS,
        )
        assert result is True

    def test_rm_requires_approval(self) -> None:
        result = requires_approval(
            "run_local_command",
            {"command": "rm /tmp/test.txt"},
            APPROVAL_PATTERNS,
        )
        assert result is True

    def test_kill_requires_approval(self) -> None:
        result = requires_approval(
            "run_local_command",
            {"command": "kill -9 1234"},
            APPROVAL_PATTERNS,
        )
        assert result is True

    def test_delete_requires_approval(self) -> None:
        result = requires_approval(
            "run_remote_command",
            {"command": "docker delete container"},
            APPROVAL_PATTERNS,
        )
        assert result is True

    def test_docker_ps_no_approval(self) -> None:
        result = requires_approval(
            "docker_ps",
            {"server": "localhost"},
            APPROVAL_PATTERNS,
        )
        assert result is False

    def test_df_no_approval(self) -> None:
        result = requires_approval(
            "run_local_command",
            {"command": "df -h"},
            APPROVAL_PATTERNS,
        )
        assert result is False

    def test_always_safe_tools_skip_approval(self) -> None:
        """Tools in ALWAYS_SAFE_TOOLS never require approval."""
        for tool in ALWAYS_SAFE_TOOLS:
            result = requires_approval(
                tool,
                {"command": "restart everything delete all"},
                APPROVAL_PATTERNS,
            )
            assert result is False, f"{tool} should be always safe"

    def test_case_insensitive_matching(self) -> None:
        result = requires_approval(
            "run_local_command",
            {"command": "DOCKER RESTART my-app"},
            APPROVAL_PATTERNS,
        )
        assert result is True

    def test_nested_dict_triggers_approval(self) -> None:
        """Approval should detect patterns in nested structures."""
        result = requires_approval(
            "complex_tool",
            {"options": {"action": "restart"}},
            APPROVAL_PATTERNS,
        )
        assert result is True

    def test_nested_list_triggers_approval(self) -> None:
        result = requires_approval(
            "complex_tool",
            {"args": ["safe", "delete this"]},
            APPROVAL_PATTERNS,
        )
        assert result is True

    def test_deeply_nested_triggers_approval(self) -> None:
        result = requires_approval(
            "complex_tool",
            {"config": {"inner": {"deep": "please stop the service"}}},
            APPROVAL_PATTERNS,
        )
        assert result is True

    def test_no_string_values_no_approval(self) -> None:
        result = requires_approval(
            "some_tool",
            {"count": 42, "enabled": True},
            APPROVAL_PATTERNS,
        )
        assert result is False

    def test_empty_patterns_no_approval(self) -> None:
        result = requires_approval(
            "run_local_command",
            {"command": "rm -rf /"},
            [],
        )
        assert result is False


# --- _extract_string_values ---


class TestExtractStringValues:
    """Tests for recursive string extraction."""

    def test_flat_dict(self) -> None:
        result = _extract_string_values({"a": "hello", "b": "world"})
        assert sorted(result) == ["hello", "world"]

    def test_nested_dict(self) -> None:
        result = _extract_string_values({"outer": {"inner": "value"}})
        assert result == ["value"]

    def test_list_values(self) -> None:
        result = _extract_string_values({"items": ["a", "b", "c"]})
        assert result == ["a", "b", "c"]

    def test_mixed_types(self) -> None:
        result = _extract_string_values({"s": "text", "n": 42, "b": True, "none": None})
        assert result == ["text"]

    def test_deeply_nested(self) -> None:
        result = _extract_string_values({
            "level1": {
                "level2": {
                    "level3": ["deep_value"]
                }
            }
        })
        assert result == ["deep_value"]

    def test_tuple_support(self) -> None:
        result = _extract_string_values({"t": ("x", "y")})
        assert result == ["x", "y"]

    def test_empty_dict(self) -> None:
        assert _extract_string_values({}) == []

    def test_string_input(self) -> None:
        assert _extract_string_values("hello") == ["hello"]
