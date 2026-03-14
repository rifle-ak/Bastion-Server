"""Tests for incident_timeline helpers."""

from __future__ import annotations

from agent.tools.incident_timeline import (
    _extract_dmesg_timestamp,
    _extract_timestamp,
    _get_severity_icon,
)


class TestExtractTimestamp:
    def test_iso_format(self):
        assert _extract_timestamp("2024-01-15T14:23:45 error msg") == "2024-01-15T14:23:45"

    def test_syslog_format(self):
        assert _extract_timestamp("Mar 14 12:34:56 host msg") == "Mar 14 12:34:56"

    def test_no_timestamp(self):
        assert _extract_timestamp("just a message") == ""


class TestExtractDmesgTimestamp:
    def test_dmesg_timestamp(self):
        assert _extract_dmesg_timestamp("[Mon Mar 14 12:00:00 2024] msg") == "Mon Mar 14 12:00:00 2024"

    def test_no_brackets(self):
        assert _extract_dmesg_timestamp("no timestamp here") == ""


class TestGetSeverityIcon:
    def test_fatal(self):
        assert _get_severity_icon("fatal error occurred") == "✗"

    def test_oom(self):
        assert _get_severity_icon("Out of memory: Killed process") == "✗"

    def test_warning(self):
        assert _get_severity_icon("warning: timeout detected") == "⚠"

    def test_normal(self):
        assert _get_severity_icon("server started successfully") == " "
