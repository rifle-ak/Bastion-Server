"""Tests for cron_audit helper functions."""

from __future__ import annotations

from agent.tools.base import ToolResult
from agent.tools.cron_audit import (
    _build_cron_report,
    _find_overlaps,
    _parse_crontab,
)


def _ok(output: str = "") -> ToolResult:
    return ToolResult(output=output)


def _err() -> ToolResult:
    return ToolResult(error="fail", exit_code=1)


class TestParseCrontab:
    def test_standard_entry(self):
        content = "0 2 * * * /usr/bin/backup\n"
        jobs = _parse_crontab(content, "root")
        assert len(jobs) == 1
        assert jobs[0]["schedule"] == "0 2 * * *"
        assert jobs[0]["command"] == "/usr/bin/backup"
        assert jobs[0]["user"] == "root"

    def test_skip_comments(self):
        content = "# This is a comment\n0 2 * * * /usr/bin/backup\n"
        jobs = _parse_crontab(content, "root")
        assert len(jobs) == 1

    def test_skip_env_vars(self):
        content = "MAILTO=admin@test.com\n0 2 * * * /usr/bin/backup\n"
        jobs = _parse_crontab(content, "root")
        assert len(jobs) == 1

    def test_empty(self):
        assert _parse_crontab("", "root") == []

    def test_multiple_entries(self):
        content = "0 2 * * * /usr/bin/backup\n*/5 * * * * /usr/bin/check\n"
        jobs = _parse_crontab(content, "user1")
        assert len(jobs) == 2
        assert jobs[0]["user"] == "user1"


class TestFindOverlaps:
    def test_overlapping_schedules(self):
        jobs = [
            {"schedule": "0 2 * * *", "command": "/usr/bin/backup"},
            {"schedule": "0 2 * * *", "command": "/usr/bin/cleanup"},
        ]
        overlaps = _find_overlaps(jobs)
        assert len(overlaps) == 1
        assert "2 jobs simultaneously" in overlaps[0]

    def test_no_overlaps(self):
        jobs = [
            {"schedule": "0 2 * * *", "command": "/usr/bin/backup"},
            {"schedule": "0 3 * * *", "command": "/usr/bin/cleanup"},
        ]
        assert _find_overlaps(jobs) == []

    def test_empty(self):
        assert _find_overlaps([]) == []


class TestBuildCronReport:
    def test_basic_report(self):
        data = {
            "system_crontab": _ok("0 2 * * * root /usr/bin/backup\n"),
            "cron_d": _ok("total 4\n-rw-r--r-- 1 root root 100 logrotate\n"),
            "cron_d_contents": _ok(""),
            "user_crons": _ok(""),
            "user_crons_alt": _ok(""),
            "cron_status": _ok("active"),
            "cron_log": _ok(""),
            "syslog_cron": _ok(""),
            "timers": _ok(""),
            "cron_hourly": _ok("script1\n"),
            "cron_daily": _ok("logrotate\nbackup\n"),
        }
        report = _build_cron_report("test-srv", data)
        assert "test-srv" in report
        assert "Cron service: active" in report
        assert "cron.hourly" in report

    def test_cron_not_running(self):
        data = {
            "system_crontab": _ok(""),
            "cron_d": _ok(""),
            "cron_d_contents": _ok(""),
            "user_crons": _ok(""),
            "user_crons_alt": _ok(""),
            "cron_status": _ok("unknown"),
            "cron_log": _ok(""),
            "syslog_cron": _ok(""),
            "timers": _ok(""),
            "cron_hourly": _ok(""),
            "cron_daily": _ok(""),
        }
        report = _build_cron_report("test-srv", data)
        assert "not active" in report
        assert "no jobs will execute" in report.lower()

    def test_cron_errors_detected(self):
        data = {
            "system_crontab": _ok(""),
            "cron_d": _ok(""),
            "cron_d_contents": _ok(""),
            "user_crons": _ok(""),
            "user_crons_alt": _ok(""),
            "cron_status": _ok("active"),
            "cron_log": _ok("error: failed to run job\npermission denied: /usr/local/bin/task\n"),
            "syslog_cron": _ok(""),
            "timers": _ok(""),
            "cron_hourly": _ok(""),
            "cron_daily": _ok(""),
        }
        report = _build_cron_report("test-srv", data)
        assert "Cron Errors" in report

    def test_systemd_timers(self):
        data = {
            "system_crontab": _ok(""),
            "cron_d": _ok(""),
            "cron_d_contents": _ok(""),
            "user_crons": _ok(""),
            "user_crons_alt": _ok(""),
            "cron_status": _ok("active"),
            "cron_log": _ok(""),
            "syslog_cron": _ok(""),
            "timers": _ok("NEXT  LEFT  LAST  PASSED  UNIT  ACTIVATES\nMon  1h  Sun  23h  logrotate.timer  logrotate.service\n"),
            "cron_hourly": _ok(""),
            "cron_daily": _ok(""),
        }
        report = _build_cron_report("test-srv", data)
        assert "Systemd Timers" in report
