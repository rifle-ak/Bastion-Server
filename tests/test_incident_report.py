"""Tests for incident_report prompt builder."""

from __future__ import annotations

from agent.tools.base import ToolResult
from agent.tools.incident_report import _build_report_prompt


def _ok(output: str = "") -> ToolResult:
    return ToolResult(output=output)


def _err() -> ToolResult:
    return ToolResult(error="fail", exit_code=1)


class TestBuildReportPrompt:
    def test_minimal_params(self):
        params = {
            "incident_summary": "Database outage",
            "severity": "critical",
        }
        prompt = _build_report_prompt(params)
        assert "Database outage" in prompt
        assert "CRITICAL" in prompt
        assert "Incident Report" in prompt
        assert "Timeline" in prompt
        assert "Root Cause Analysis" in prompt
        assert "Preventive Measures" in prompt

    def test_full_params(self):
        params = {
            "incident_summary": "Web server crash",
            "severity": "major",
            "service_type": "website",
            "affected_servers": "web-01, web-02",
            "root_cause": "Disk full",
            "resolution": "Cleaned old logs",
            "start_time": "2024-01-15 14:30 UTC",
            "end_time": "2024-01-15 15:45 UTC",
            "customer_facing": True,
        }
        prompt = _build_report_prompt(params)
        assert "web-01, web-02" in prompt
        assert "Disk full" in prompt
        assert "Cleaned old logs" in prompt
        assert "14:30 UTC" in prompt
        assert "15:45 UTC" in prompt
        assert "Customer Communication" in prompt
        assert "MAJOR" in prompt

    def test_duration_with_start_only(self):
        params = {
            "incident_summary": "Ongoing issue",
            "severity": "minor",
            "start_time": "2024-01-15 14:30 UTC",
        }
        prompt = _build_report_prompt(params)
        assert "14:30 UTC to ongoing" in prompt

    def test_duration_no_times(self):
        params = {
            "incident_summary": "Unknown timing",
            "severity": "minor",
        }
        prompt = _build_report_prompt(params)
        assert "Determine from conversation context" in prompt

    def test_game_server_guidance(self):
        params = {
            "incident_summary": "Game server crash",
            "severity": "critical",
            "service_type": "game_server",
        }
        prompt = _build_report_prompt(params)
        assert "Platform-Specific Guidance" in prompt
        assert "player impact" in prompt
        assert "Pterodactyl" in prompt

    def test_email_guidance(self):
        params = {
            "incident_summary": "Mail queue overflow",
            "severity": "major",
            "service_type": "email",
        }
        prompt = _build_report_prompt(params)
        assert "mail queue" in prompt
        assert "SPF/DKIM/DMARC" in prompt

    def test_no_customer_section_when_disabled(self):
        params = {
            "incident_summary": "Internal issue",
            "severity": "minor",
            "customer_facing": False,
        }
        prompt = _build_report_prompt(params)
        assert "Customer Communication" not in prompt

    def test_no_platform_guidance_for_unknown_type(self):
        params = {
            "incident_summary": "Something broke",
            "severity": "minor",
            "service_type": "",
        }
        prompt = _build_report_prompt(params)
        assert "Platform-Specific Guidance" not in prompt

    def test_infrastructure_guidance(self):
        params = {
            "incident_summary": "Network partition",
            "severity": "critical",
            "service_type": "infrastructure",
        }
        prompt = _build_report_prompt(params)
        assert "cascade failure" in prompt
        assert "single points of failure" in prompt
