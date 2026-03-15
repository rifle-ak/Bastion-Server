"""Tests for anomaly detection."""

from __future__ import annotations

from agent.anomaly import Anomaly, AnomalyReport


class TestAnomalyReport:
    def test_empty_report(self):
        report = AnomalyReport(checked_servers=5, elapsed=2.0)
        assert not report.has_issues
        assert report.critical_count == 0
        assert "clean" in report.format()

    def test_report_with_issues(self):
        report = AnomalyReport(
            anomalies=[
                Anomaly(server="srv1", category="container", severity="critical",
                        message="Container X in restart loop"),
                Anomaly(server="srv1", category="disk", severity="warning",
                        message="Disk growing fast", value="5000 MB/day"),
            ],
            checked_servers=3,
            elapsed=1.5,
        )
        assert report.has_issues
        assert report.critical_count == 1
        assert report.warning_count == 1

        text = report.format()
        assert "srv1" in text
        assert "restart loop" in text
        assert "growing fast" in text

    def test_format_groups_by_server(self):
        report = AnomalyReport(
            anomalies=[
                Anomaly(server="srv1", category="disk", severity="warning", message="disk issue"),
                Anomaly(server="srv2", category="cpu", severity="critical", message="cpu issue"),
                Anomaly(server="srv1", category="mem", severity="warning", message="mem issue"),
            ],
            checked_servers=2,
            elapsed=1.0,
        )
        text = report.format()
        assert "srv1" in text
        assert "srv2" in text
