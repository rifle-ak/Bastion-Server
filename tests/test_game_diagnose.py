"""Tests for game_diagnose helper functions."""

from __future__ import annotations

from agent.tools.base import ToolResult
from agent.tools.game_diagnose import (
    _build_game_report,
    _detect_crashes,
    _detect_gc_issues,
    _detect_tick_issues,
    _extract_throttle,
    _find_noisy_neighbors,
)


def _ok(output: str = "") -> ToolResult:
    return ToolResult(output=output)


def _err(error: str = "fail") -> ToolResult:
    return ToolResult(error=error, exit_code=1)


class TestExtractThrottle:
    def test_cgroup_v2(self):
        assert _extract_throttle("usage_usec 1234\nthrottled_usec 5678\n") == 5678

    def test_cgroup_v1(self):
        assert _extract_throttle("nr_periods 100\nnr_throttled 42\n") == 42

    def test_no_throttle(self):
        assert _extract_throttle("some_other_stat 99\n") is None

    def test_empty(self):
        assert _extract_throttle("") is None

    def test_zero(self):
        assert _extract_throttle("throttled_usec 0\n") == 0


class TestDetectGcIssues:
    def test_java_gc_pause(self):
        logs = "GC pause (G1 Evacuation) 250ms\nGC pause (G1 Mixed) 50ms\n"
        issues = _detect_gc_issues(logs)
        assert len(issues) >= 1
        assert "GC PAUSES" in issues[0]
        assert "250" in issues[0]

    def test_no_gc_issues(self):
        assert _detect_gc_issues("Server started\nPlayer joined\n") == []

    def test_short_gc_ok(self):
        logs = "GC pause 20ms\nGC pause 30ms\n"
        assert _detect_gc_issues(logs) == []


class TestDetectTickIssues:
    def test_cant_keep_up(self):
        logs = "\n".join(["Can't keep up! Is the server overloaded?"] * 10)
        issues = _detect_tick_issues(logs)
        assert any("TICK LAG" in i for i in issues)

    def test_few_cant_keep_up(self):
        logs = "Can't keep up! Is the server overloaded?\nNormal line\n"
        issues = _detect_tick_issues(logs)
        assert any("Can't keep up" in i for i in issues)
        assert not any("TICK LAG" in i for i in issues)

    def test_took_too_long(self):
        logs = "Plugin.OnPlayerConnected took too long (500ms)\n"
        issues = _detect_tick_issues(logs)
        assert any("took too long" in i for i in issues)

    def test_clean_logs(self):
        assert _detect_tick_issues("Player joined\nWorld saved\n") == []


class TestDetectCrashes:
    def test_segfault(self):
        logs = "normal line\nSegmentation fault (core dumped)\n"
        issues = _detect_crashes(logs)
        assert len(issues) == 1
        assert "CRASH" in issues[0]

    def test_oom(self):
        issues = _detect_crashes("java.lang.OutOfMemoryError: heap space\n")
        assert len(issues) == 1

    def test_no_crash(self):
        assert _detect_crashes("Server running fine\nPlayer joined\n") == []


class TestFindNoisyNeighbors:
    def test_finds_noisy(self):
        all_containers = "mc_server|15.2%|30.5%\nrust_server|55.0%|40.0%\nweb|2.0%|5.0%\n"
        noisy = _find_noisy_neighbors(all_containers, "mc_server")
        assert len(noisy) == 1  # rust_server (web is <10%)
        assert noisy[0][0] == "rust_server"

    def test_excludes_target(self):
        all_containers = "target|90.0%|80.0%\nother|5.0%|10.0%\n"
        noisy = _find_noisy_neighbors(all_containers, "target")
        assert len(noisy) == 0

    def test_empty(self):
        assert _find_noisy_neighbors("", "target") == []

    def test_malformed_lines(self):
        assert _find_noisy_neighbors("bad line\n", "target") == []


class TestBuildGameReport:
    def test_healthy_server(self):
        data = {
            "stats": _ok("25.0%|1.5GiB / 4GiB|37.5%|100MB / 50MB|200MB / 100MB|45"),
            "throttling": _ok("throttled_usec 0\n"),
            "throttling_v1": _err(),
            "mem_limit": _ok("4294967296"),
            "mem_current": _ok("1610612736"),
            "mem_swap": _ok("0"),
            "iowait": _ok("Cpu(s):  5.0 us,  1.0 sy,  0.0 ni, 92.0 id,  1.5 wa"),
            "iostat": _err(),
            "tcp_retrans": _ok("retrans:0/5"),
            "net_errors": _ok(""),
            "processes": _ok(""),
            "logs": _ok("Server started\nPlayer joined"),
            "uptime": _ok("12:00  up 5 days, load average: 2.0, 1.5, 1.0"),
            "nproc": _ok("8"),
            "all_containers": _ok("test_server|25.0%|37.5%\n"),
            "inspect": _ok("200000|100000|4294967296|8589934592|2024-01-01T00:00:00Z|0"),
            "dmesg_oom": _ok(""),
        }
        report = _build_game_report("test_server", data)
        assert "test_server" in report
        assert "No CPU throttling" in report
        assert "No performance issues" in report

    def test_throttled_server(self):
        data = {
            "stats": _ok("95.0%|3.8GiB / 4GiB|95.0%|1GB / 500MB|5GB / 2GB|120"),
            "throttling": _ok("throttled_usec 50000\n"),
            "throttling_v1": _err(),
            "mem_limit": _ok("4294967296"),
            "mem_current": _ok("4080218931"),  # ~95%
            "mem_swap": _ok("104857600"),  # 100MB in swap
            "iowait": _ok("Cpu(s):  5.0 us,  1.0 sy,  0.0 ni, 62.0 id,  30.0 wa"),
            "iostat": _err(),
            "tcp_retrans": _ok("retrans:5/150"),
            "net_errors": _ok(""),
            "processes": _ok(""),
            "logs": _ok("\n".join(["Can't keep up!"] * 10)),
            "uptime": _ok("12:00  up 1 day, load average: 20.0, 18.0, 15.0"),
            "nproc": _ok("4"),
            "all_containers": _ok("game|95.0%|95.0%\nother|60.0%|50.0%\n"),
            "inspect": _ok("200000|100000|4294967296|0|2024-01-01T00:00:00Z|3"),
            "dmesg_oom": _ok(""),
        }
        report = _build_game_report("game", data)
        assert "THROTTLED" in report
        assert "MEMORY CRITICAL" in report
        assert "SWAPPING" in report
        assert "I/O WAIT" in report
        assert "TICK LAG" in report
        assert "CPU throttling" in report  # Root cause
