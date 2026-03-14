"""Tests for the health check tool's parsing and analysis functions."""

from __future__ import annotations

import pytest

from agent.tools.health import _parse_containers, _parse_disk, _parse_memory


class TestParseDisk:
    """Tests for df -h output parsing."""

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
        assert "/" in issues[0]

    def test_skips_snap_mounts(self):
        output = (
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "/dev/loop0       64M   64M     0 100% /snap/core20/123\n"
        )
        assert _parse_disk(output) == []

    def test_multiple_issues(self):
        output = (
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "/dev/sda1        50G   45G   3.5G  91% /\n"
            "/dev/sdb1       100G   85G   12G   85% /data\n"
        )
        issues = _parse_disk(output)
        assert len(issues) == 2


class TestParseMemory:
    """Tests for free -m output parsing."""

    def test_healthy_memory(self):
        output = (
            "              total        used        free      shared  buff/cache   available\n"
            "Mem:          16000        8000        4000         200        4000       10000\n"
            "Swap:          2000           0        2000\n"
        )
        assert _parse_memory(output) is None

    def test_high_memory(self):
        output = (
            "              total        used        free      shared  buff/cache   available\n"
            "Mem:          16000       14000         500         200        1500        1000\n"
            "Swap:          2000           0        2000\n"
        )
        result = _parse_memory(output)
        assert result is not None
        assert "88%" in result or "87%" in result

    def test_empty_output(self):
        assert _parse_memory("") is None


class TestParseContainers:
    """Tests for docker ps output parsing."""

    def test_all_running(self):
        output = (
            "NAMES\tSTATUS\tSTATE\n"
            "web\tUp 3 hours\trunning\n"
            "db\tUp 3 hours\trunning\n"
        )
        assert _parse_containers(output) == []

    def test_exited_container(self):
        output = "abc123\tExited (1) 5 min ago\texited\n"
        issues = _parse_containers(output)
        assert len(issues) == 1
        assert "exited" in issues[0].lower()

    def test_restarting_container(self):
        output = "game01\tRestarting (1) 10 seconds ago\trestarting\n"
        issues = _parse_containers(output)
        assert len(issues) == 1
        assert "restarting" in issues[0].lower()
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
        issues = _parse_containers(output)
        assert len(issues) == 2
