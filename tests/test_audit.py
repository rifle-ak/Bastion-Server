"""Tests for the audit logger and ToolResult."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from agent.security.audit import AuditLogger, _truncate_result
from agent.tools.base import ToolResult


# --- AuditLogger ---


class TestAuditLogger:
    """Tests for the structured audit logger."""

    def test_log_attempt_writes_jsonl(self, tmp_path: Path) -> None:
        log_file = tmp_path / "audit.jsonl"
        with AuditLogger(str(log_file)) as audit:
            audit.log_attempt("run_local_command", {"command": "uptime"})

        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["event"] == "tool_attempt"
        assert entry["tool"] == "run_local_command"
        assert entry["input"] == {"command": "uptime"}
        assert "timestamp" in entry

    def test_log_success(self, tmp_path: Path) -> None:
        log_file = tmp_path / "audit.jsonl"
        with AuditLogger(str(log_file)) as audit:
            audit.log_success(
                "run_local_command",
                {"command": "uptime"},
                {"output": "up 5 days", "exit_code": 0},
            )

        entry = json.loads(log_file.read_text().strip())
        assert entry["event"] == "tool_success"
        assert entry["result"]["output"] == "up 5 days"

    def test_log_denied(self, tmp_path: Path) -> None:
        log_file = tmp_path / "audit.jsonl"
        with AuditLogger(str(log_file)) as audit:
            audit.log_denied(
                "run_local_command",
                {"command": "rm -rf /"},
                reason="allowlist",
            )

        entry = json.loads(log_file.read_text().strip())
        assert entry["event"] == "tool_denied"
        assert entry["reason"] == "allowlist"

    def test_log_error(self, tmp_path: Path) -> None:
        log_file = tmp_path / "audit.jsonl"
        with AuditLogger(str(log_file)) as audit:
            audit.log_error(
                "run_local_command",
                {"command": "fail"},
                error="something broke",
            )

        entry = json.loads(log_file.read_text().strip())
        assert entry["event"] == "tool_error"
        assert entry["error"] == "something broke"

    def test_log_timeout(self, tmp_path: Path) -> None:
        log_file = tmp_path / "audit.jsonl"
        with AuditLogger(str(log_file)) as audit:
            audit.log_timeout("slow_tool", {"command": "sleep 999"})

        entry = json.loads(log_file.read_text().strip())
        assert entry["event"] == "tool_timeout"

    def test_multiple_entries(self, tmp_path: Path) -> None:
        log_file = tmp_path / "audit.jsonl"
        with AuditLogger(str(log_file)) as audit:
            audit.log_attempt("tool_a", {})
            audit.log_attempt("tool_b", {})
            audit.log_success("tool_a", {}, {"output": "ok"})

        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 3

    def test_context_manager_closes_file(self, tmp_path: Path) -> None:
        log_file = tmp_path / "audit.jsonl"
        audit = AuditLogger(str(log_file))
        assert not audit._file.closed
        audit.__exit__(None, None, None)
        assert audit._file.closed

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        log_file = tmp_path / "subdir" / "deep" / "audit.jsonl"
        with AuditLogger(str(log_file)) as audit:
            audit.log_attempt("test", {})
        assert log_file.exists()


# --- _truncate_result ---


class TestTruncateResult:
    """Tests for result truncation."""

    def test_short_values_unchanged(self) -> None:
        result = {"output": "hello", "exit_code": 0}
        assert _truncate_result(result) == result

    def test_long_string_truncated(self) -> None:
        long_str = "x" * 3000
        result = _truncate_result({"output": long_str})
        assert len(result["output"]) < 3000
        assert "truncated" in result["output"]
        assert "3000 total" in result["output"]

    def test_non_string_values_unchanged(self) -> None:
        result = {"exit_code": 0, "count": 42}
        assert _truncate_result(result) == result

    def test_custom_max_len(self) -> None:
        result = _truncate_result({"output": "x" * 100}, max_len=50)
        assert len(result["output"]) < 100
        assert "truncated" in result["output"]


# --- ToolResult ---


class TestToolResult:
    """Tests for the ToolResult data class."""

    def test_to_dict_always_includes_output(self) -> None:
        result = ToolResult(output="", exit_code=0)
        d = result.to_dict()
        assert "output" in d
        assert d["output"] == ""

    def test_to_dict_with_output(self) -> None:
        result = ToolResult(output="hello world", exit_code=0)
        d = result.to_dict()
        assert d["output"] == "hello world"
        assert d["exit_code"] == 0
        assert "error" not in d

    def test_to_dict_with_error(self) -> None:
        result = ToolResult(error="something failed", exit_code=1)
        d = result.to_dict()
        assert d["error"] == "something failed"
        assert d["exit_code"] == 1
        assert d["output"] == ""

    def test_to_dict_with_both(self) -> None:
        result = ToolResult(output="partial", error="warning", exit_code=0)
        d = result.to_dict()
        assert d["output"] == "partial"
        assert d["error"] == "warning"

    def test_success_property_true(self) -> None:
        assert ToolResult(output="ok", exit_code=0).success is True

    def test_success_property_false_on_error(self) -> None:
        assert ToolResult(error="fail", exit_code=1).success is False

    def test_success_property_false_on_nonzero_exit(self) -> None:
        assert ToolResult(output="", exit_code=1).success is False

    def test_success_property_false_on_error_string(self) -> None:
        """Even with exit_code=0, having an error string means not successful."""
        assert ToolResult(output="", error="warning", exit_code=0).success is False

    def test_frozen_dataclass(self) -> None:
        result = ToolResult(output="test", exit_code=0)
        with pytest.raises(AttributeError):
            result.output = "modified"  # type: ignore[misc]
