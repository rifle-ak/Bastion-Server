"""Tests for what_changed report builder."""

from __future__ import annotations

from agent.tools.base import ToolResult
from agent.tools.what_changed import _build_changes_report


def _ok(output: str = "") -> ToolResult:
    return ToolResult(output=output)


def _err() -> ToolResult:
    return ToolResult(error="fail", exit_code=1)


class TestBuildChangesReport:
    def test_no_changes(self):
        data = {k: _ok("") for k in [
            "apt_history", "yum_history", "config_changes", "docker_events",
            "docker_images", "service_changes", "cron_changes", "logins",
            "wings_version", "pterodactyl_changes", "reboots",
        ]}
        report = _build_changes_report("srv1", 24, data)
        assert "No significant changes" in report

    def test_package_changes(self):
        data = {k: _ok("") for k in [
            "apt_history", "yum_history", "config_changes", "docker_events",
            "docker_images", "service_changes", "cron_changes", "logins",
            "wings_version", "pterodactyl_changes", "reboots",
        ]}
        data["apt_history"] = _ok("Upgrade: nginx 1.18 -> 1.20\nUpgrade: openssl 1.1 -> 3.0\n")
        report = _build_changes_report("srv1", 24, data)
        assert "Package Changes" in report
        assert "nginx" in report

    def test_config_changes(self):
        data = {k: _ok("") for k in [
            "apt_history", "yum_history", "config_changes", "docker_events",
            "docker_images", "service_changes", "cron_changes", "logins",
            "wings_version", "pterodactyl_changes", "reboots",
        ]}
        data["config_changes"] = _ok("/etc/nginx/nginx.conf\n/etc/ssh/sshd_config\n")
        report = _build_changes_report("srv1", 24, data)
        assert "Config Files Modified" in report
        assert "nginx.conf" in report

    def test_docker_events(self):
        data = {k: _ok("") for k in [
            "apt_history", "yum_history", "config_changes", "docker_events",
            "docker_images", "service_changes", "cron_changes", "logins",
            "wings_version", "pterodactyl_changes", "reboots",
        ]}
        data["docker_events"] = _ok("2024-01-15T12:00:00 restart mc_server\n2024-01-15T12:01:00 start mc_server\n")
        report = _build_changes_report("srv1", 24, data)
        assert "Docker Events" in report
        assert "mc_server" in report

    def test_reboot_detected(self):
        data = {k: _ok("") for k in [
            "apt_history", "yum_history", "config_changes", "docker_events",
            "docker_images", "service_changes", "cron_changes", "logins",
            "wings_version", "pterodactyl_changes", "reboots",
        ]}
        data["reboots"] = _ok("reboot   system boot  5.15.0  Mon Jan 15 12:00\n")
        report = _build_changes_report("srv1", 24, data)
        assert "Reboot" in report
        assert "changes detected" in report

    def test_multiple_changes(self):
        data = {k: _ok("") for k in [
            "apt_history", "yum_history", "config_changes", "docker_events",
            "docker_images", "service_changes", "cron_changes", "logins",
            "wings_version", "pterodactyl_changes", "reboots",
        ]}
        data["apt_history"] = _ok("Upgrade: pkg1\n")
        data["config_changes"] = _ok("/etc/foo\n")
        data["docker_events"] = _ok("event1\n")
        report = _build_changes_report("srv1", 12, data)
        assert "3 changes detected" in report
        assert "last 12 hours" in report
