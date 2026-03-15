"""Tests for health check parsers and webhost analysis."""

from __future__ import annotations

import pytest

from agent.tools.health import (
    _analyze_webhost,
    _count_oom,
    _parse_containers,
    _parse_disk,
    _parse_iowait,
    _parse_memory,
    _parse_tcp_connections,
)


class TestParseDisk:
    def test_healthy_disk(self):
        output = (
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "/dev/sda1        50G   20G   28G  42% /\n"
            "tmpfs           2.0G     0  2.0G   0% /dev/shm\n"
        )
        assert _parse_disk(output) == []

    def test_high_disk_usage(self):
        output = (
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "/dev/sda1        50G   45G   3.5G  91% /\n"
        )
        issues = _parse_disk(output)
        assert len(issues) == 1
        assert "91%" in issues[0]

    def test_skips_snap_mounts(self):
        output = (
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "/dev/loop0       64M   64M     0 100% /snap/core20/123\n"
        )
        assert _parse_disk(output) == []

    def test_skips_run_mounts(self):
        output = (
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "tmpfs           100M   90M   10M  90% /run/user/1000\n"
        )
        assert _parse_disk(output) == []

    def test_multiple_issues(self):
        output = (
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "/dev/sda1        50G   45G   3.5G  91% /\n"
            "/dev/sdb1       100G   85G   12G   85% /data\n"
        )
        assert len(_parse_disk(output)) == 2


class TestParseMemory:
    def test_healthy_memory(self):
        output = (
            "              total        used        free      shared  buff/cache   available\n"
            "Mem:          16000        8000        4000         200        4000       10000\n"
        )
        assert _parse_memory(output) is None

    def test_high_memory(self):
        output = (
            "              total        used        free\n"
            "Mem:          16000       14000         500\n"
        )
        result = _parse_memory(output)
        assert result is not None
        assert "%" in result

    def test_empty_output(self):
        assert _parse_memory("") is None


class TestParseContainers:
    def test_all_running(self):
        output = (
            "NAMES\tSTATUS\tSTATE\n"
            "web\tUp 3 hours\trunning\n"
        )
        assert _parse_containers(output) == []

    def test_exited_container(self):
        output = "abc123\tExited (1) 5 min ago\texited\n"
        issues = _parse_containers(output)
        assert len(issues) == 1
        assert "exited" in issues[0].lower()

    def test_restarting_container(self):
        output = "game01\tRestarting (1) 10s ago\trestarting\n"
        issues = _parse_containers(output)
        assert len(issues) == 1
        assert "crash loop" in issues[0].lower()

    def test_unhealthy_container(self):
        output = "wings\tUp 3 hours (unhealthy)\trunning\n"
        issues = _parse_containers(output)
        assert len(issues) == 1
        assert "unhealthy" in issues[0].lower()

    def test_mixed_states(self):
        output = (
            "web\tUp 3 hours\trunning\n"
            "game01\tExited (137)\texited\n"
            "game02\tRestarting\trestarting\n"
        )
        assert len(_parse_containers(output)) == 2


class TestParseIowait:
    def test_normal_iowait(self):
        line = "%Cpu(s):  2.3 us,  1.0 sy,  0.0 ni, 95.5 id,  1.2 wa,  0.0 hi"
        assert _parse_iowait(line) == pytest.approx(1.2)

    def test_high_iowait(self):
        line = "%Cpu(s):  5.0 us,  2.0 sy,  0.0 ni, 70.0 id, 23.0 wa,  0.0 hi"
        assert _parse_iowait(line) == pytest.approx(23.0)

    def test_no_cpu_line(self):
        assert _parse_iowait("some other output") is None

    def test_multiline_top(self):
        output = (
            "top - 14:00:00 up 5 days\n"
            "Tasks: 200 total\n"
            "%Cpu(s):  3.0 us,  1.0 sy,  0.0 ni, 90.0 id,  6.0 wa,  0.0 hi\n"
        )
        assert _parse_iowait(output) == pytest.approx(6.0)


class TestCountOom:
    def test_no_oom(self):
        assert _count_oom("") == 0
        assert _count_oom("normal dmesg output\nanother line") == 0

    def test_oom_kills(self):
        output = (
            "[Thu Jan  1 12:00:00 2026] Out of memory: Killed process 1234\n"
            "[Thu Jan  1 12:05:00 2026] oom-kill:constraint=CONSTRAINT_NONE\n"
            "[Thu Jan  1 12:10:00 2026] some normal message\n"
        )
        assert _count_oom(output) == 2


class TestParseTcpConnections:
    def test_normal_output(self):
        output = (
            "Total: 150\n"
            "TCP:   85 (estab 42, closed 5, orphaned 0, timewait 38)\n"
        )
        assert _parse_tcp_connections(output) == 42

    def test_high_connections(self):
        output = "TCP:   2000 (estab 1500, closed 100, orphaned 10)\n"
        assert _parse_tcp_connections(output) == 1500

    def test_no_tcp_line(self):
        assert _parse_tcp_connections("some other output") is None

    def test_empty(self):
        assert _parse_tcp_connections("") is None


class TestAnalyzeWebhost:
    def test_mysql_normal(self):
        raw = {"mysql_status": "Uptime: 86400  Threads: 5  Slow queries: 0"}
        lines: list[str] = []
        issues: list[str] = []
        _analyze_webhost(raw, lines, issues)
        assert any("MySQL" in l for l in lines)
        assert len(issues) == 0

    def test_mysql_high_threads(self):
        raw = {"mysql_status": "Uptime: 86400  Threads: 150  Slow queries: 0"}
        lines: list[str] = []
        issues: list[str] = []
        _analyze_webhost(raw, lines, issues)
        assert any("high threads" in i for i in issues)

    def test_mysql_slow_queries(self):
        raw = {"mysql_status": "Uptime: 86400  Threads: 5  Slow queries: 100"}
        lines: list[str] = []
        issues: list[str] = []
        _analyze_webhost(raw, lines, issues)
        assert any("slow queries" in i.lower() for i in issues)

    def test_mail_queue_normal(self):
        raw = {"mail_queue": "15"}
        lines: list[str] = []
        issues: list[str] = []
        _analyze_webhost(raw, lines, issues)
        assert any("15" in l for l in lines)
        assert len(issues) == 0

    def test_mail_queue_high(self):
        raw = {"mail_queue": "750"}
        lines: list[str] = []
        issues: list[str] = []
        _analyze_webhost(raw, lines, issues)
        assert any("750" in i for i in issues)
        assert any("spam" in i.lower() or "bounce" in i.lower() for i in issues)

    def test_mail_queue_moderate(self):
        raw = {"mail_queue": "200"}
        lines: list[str] = []
        issues: list[str] = []
        _analyze_webhost(raw, lines, issues)
        assert any("200" in i for i in issues)

    def test_apache_normal(self):
        raw = {"apache_procs": "30"}
        lines: list[str] = []
        issues: list[str] = []
        _analyze_webhost(raw, lines, issues)
        assert any("30" in l for l in lines)
        assert len(issues) == 0

    def test_apache_high(self):
        raw = {"apache_procs": "350"}
        lines: list[str] = []
        issues: list[str] = []
        _analyze_webhost(raw, lines, issues)
        assert any("350" in i for i in issues)
        assert any("DDoS" in i or "MaxClients" in i for i in issues)

    def test_empty_raw(self):
        lines: list[str] = []
        issues: list[str] = []
        _analyze_webhost({}, lines, issues)
        assert lines == []
        assert issues == []
