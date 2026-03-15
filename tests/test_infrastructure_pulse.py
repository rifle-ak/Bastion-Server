"""Tests for infrastructure_pulse helper functions."""

from __future__ import annotations

from agent.tools.base import ToolResult
from agent.tools.infrastructure_pulse import _build_pulse, _extract_value


def _ok(output: str = "") -> ToolResult:
    return ToolResult(output=output)


def _err() -> ToolResult:
    return ToolResult(error="fail", exit_code=1)


class TestExtractValue:
    def test_basic(self):
        text = "UPTIME:up 5 days\nDISK:42%\nMEM:1024/4096MB (25%)"
        assert _extract_value(text, "DISK:") == "42%"
        assert _extract_value(text, "MEM:") == "1024/4096MB (25%)"

    def test_missing(self):
        assert _extract_value("UPTIME:foo", "DISK:") == ""

    def test_empty(self):
        assert _extract_value("", "DISK:") == ""


class TestBuildPulse:
    def test_all_healthy(self):
        data = {
            "srv1:vitals": _ok("UPTIME:up 5 days\nDISK:42%\nMEM:1024/4096MB (25%)\nLOAD:1.5 1.0 0.8"),
            "srv1:docker_issues": _ok(""),
            "srv1:dmesg_recent": _ok(""),
        }
        report = _build_pulse(["srv1"], data, 2.0)
        assert "healthy" in report.lower()
        assert "1/1 servers clean" in report or "All 1 servers healthy" in report

    def test_high_disk(self):
        data = {
            "srv1:vitals": _ok("UPTIME:up 5 days\nDISK: 92%\nMEM:1024/4096MB (25%)\nLOAD:1.0 0.8 0.5"),
            "srv1:docker_issues": _ok(""),
            "srv1:dmesg_recent": _ok(""),
        }
        report = _build_pulse(["srv1"], data, 1.0)
        assert "Disk 92%" in report

    def test_container_issues(self):
        data = {
            "srv1:vitals": _ok("UPTIME:up 2 days\nDISK:50%\nMEM:2048/4096MB (50%)\nLOAD:2.0 1.5 1.0"),
            "srv1:docker_issues": _ok("minecraft_server|Restarting (1) 5 seconds ago"),
            "srv1:dmesg_recent": _ok(""),
        }
        report = _build_pulse(["srv1"], data, 1.5)
        assert "minecraft_server" in report

    def test_unreachable_server(self):
        data = {
            "srv1:vitals": _err(),
            "srv1:docker_issues": _err(),
            "srv1:dmesg_recent": _err(),
        }
        report = _build_pulse(["srv1"], data, 0.5)
        assert "unreachable" in report.lower()

    def test_multiple_servers(self):
        data = {
            "srv1:vitals": _ok("UPTIME:up 5 days\nDISK:30%\nMEM:512/4096MB (12%)\nLOAD:0.5 0.3 0.2"),
            "srv1:docker_issues": _ok(""),
            "srv1:dmesg_recent": _ok(""),
            "srv2:vitals": _ok("UPTIME:up 1 day\nDISK:85%\nMEM:3800/4096MB (92%)\nLOAD:8.0 7.0 6.0"),
            "srv2:docker_issues": _ok(""),
            "srv2:dmesg_recent": _ok(""),
        }
        report = _build_pulse(["srv1", "srv2"], data, 3.0)
        assert "srv2" in report
        assert "Disk 85%" in report or "Memory" in report

    def test_oom_in_dmesg(self):
        data = {
            "srv1:vitals": _ok("UPTIME:up 3 days\nDISK:40%\nMEM:1024/4096MB (25%)\nLOAD:1.0 0.8 0.5"),
            "srv1:docker_issues": _ok(""),
            "srv1:dmesg_recent": _ok("[Mon Mar 14 12:00:00 2024] Out of memory: Killed process 1234"),
        }
        report = _build_pulse(["srv1"], data, 1.0)
        assert "OOM" in report

    def test_recent_reboot(self):
        data = {
            "srv1:vitals": _ok("UPTIME: 12:00:00 up 5 min\nDISK:30%\nMEM:512/4096MB (12%)\nLOAD:0.5 0.3 0.2"),
            "srv1:docker_issues": _ok(""),
            "srv1:dmesg_recent": _ok(""),
        }
        report = _build_pulse(["srv1"], data, 1.0)
        assert "reboot" in report.lower()
