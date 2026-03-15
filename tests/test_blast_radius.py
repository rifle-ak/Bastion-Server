"""Tests for blast_radius report builder."""

from __future__ import annotations

from agent.tools.base import ToolResult
from agent.tools.blast_radius import _build_blast_report


def _ok(output: str = "") -> ToolResult:
    return ToolResult(output=output)


def _err() -> ToolResult:
    return ToolResult(error="fail", exit_code=1)


class TestBuildBlastReport:
    def _base_data(self, **overrides):
        data = {
            "containers": _ok("mc_server|Up 2 days|0.0.0.0:25565->25565/tcp\nrust_server|Up 5 days|0.0.0.0:28015->28015/tcp\n"),
            "connections": _ok("150"),
            "port_connections": _ok("  80 443\n  45 25565\n  30 28015\n  5 22\n"),
            "services": _ok("docker.service loaded active running\nnginx.service loaded active running\n"),
            "uptime": _ok("12:00:00 up 30 days"),
        }
        data.update(overrides)
        return data

    def test_reboot_critical(self):
        report = _build_blast_report("srv1", "reboot", self._base_data())
        assert "CRITICAL" in report
        assert "ALL" in report
        assert "mc_server" in report

    def test_docker_restart_critical(self):
        report = _build_blast_report("srv1", "restart docker", self._base_data())
        assert "CRITICAL" in report
        assert "ALL" in report

    def test_container_restart(self):
        data = self._base_data()
        data["container_connections"] = _ok("45")
        data["container_detail"] = _ok("2024-01-01T00:00:00Z|2|25565/tcp")
        report = _build_blast_report("srv1", "restart container mc_server", data)
        assert "45 active connections" in report
        assert "HIGH" in report

    def test_mysql_restart(self):
        report = _build_blast_report("srv1", "restart mysql", self._base_data())
        assert "MySQL" in report
        assert "database" in report.lower()

    def test_nginx_restart(self):
        report = _build_blast_report("srv1", "restart nginx", self._base_data())
        assert "Web server" in report

    def test_wings_restart(self):
        report = _build_blast_report("srv1", "restart pterodactyl-wings", self._base_data())
        assert "Wings" in report
        assert "game servers" in report.lower()
