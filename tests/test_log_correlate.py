"""Tests for log_correlate helper functions."""

from __future__ import annotations

from agent.tools.base import ToolResult
from agent.tools.log_correlate import _build_correlation_report


def _ok(output: str = "") -> ToolResult:
    return ToolResult(output=output)


def _err() -> ToolResult:
    return ToolResult(error="fail", exit_code=1)


class TestBuildCorrelationReport:
    def test_empty_logs(self):
        report = _build_correlation_report({}, None, "1h")
        assert "No log entries" in report

    def test_basic_report(self):
        data = {
            "server1:syslog": _ok("Mar 14 error: something failed\nMar 14 info: all good\n"),
            "server1:svc:nginx": _ok("error: upstream timeout\n"),
        }
        report = _build_correlation_report(data, None, "1h")
        assert "server1" in report
        assert "error" in report.lower()

    def test_keyword_filter(self):
        data = {
            "server1:syslog": _ok("timeout occurred\nnormal line\nanother timeout\n"),
        }
        report = _build_correlation_report(data, "timeout", "1h")
        assert "timeout" in report
        assert "normal line" not in report

    def test_keyword_no_match(self):
        data = {
            "server1:syslog": _ok("everything is fine\n"),
        }
        report = _build_correlation_report(data, "error", "1h")
        assert "No log entries" in report

    def test_cross_server_oom_timeout_correlation(self):
        data = {
            "server1:syslog": _ok("oom killer invoked\nfatal: out of memory\n"),
            "server2:syslog": _ok("error: connection timeout\nerror: request timed out\n"),
        }
        report = _build_correlation_report(data, None, "1h")
        assert "Cross-Server Summary" in report
        assert "oom" in report.lower()
        assert "timeout" in report.lower()
        assert "memory pressure" in report.lower()

    def test_connection_refused_correlation(self):
        data = {
            "server1:syslog": _ok("error: connection refused to db\n"),
        }
        report = _build_correlation_report(data, None, "1h")
        assert "refused" in report.lower()
        assert "service" in report.lower()

    def test_failed_results_excluded(self):
        data = {
            "server1:syslog": _err(),
            "server2:syslog": _ok("error: something\n"),
        }
        report = _build_correlation_report(data, None, "1h")
        assert "server2" in report

    def test_multi_server(self):
        data = {
            "web:syslog": _ok("error: 502 bad gateway\n"),
            "db:syslog": _ok("fatal: too many connections\n"),
            "app:container:api": _ok("panic: nil pointer\n"),
        }
        report = _build_correlation_report(data, None, "1h")
        assert "web" in report
        assert "db" in report
        assert "app" in report
