"""Tests for pterodactyl_overview report builder and parsing helpers."""

from __future__ import annotations

from agent.tools.base import ToolResult
from agent.tools.pterodactyl_overview import (
    _build_overview_report,
    _classify_status,
    _parse_docker_ps,
    _parse_docker_stats,
    _parse_disk_usage,
    _pct_value,
    _recently_restarted,
)


def _ok(output: str = "") -> ToolResult:
    return ToolResult(output=output)


def _err() -> ToolResult:
    return ToolResult(error="fail", exit_code=1)


class TestParsingHelpers:
    def test_parse_docker_ps_normal(self):
        raw = "mc_server|Up 2 days|itzg/minecraft-server\nrust_srv|Exited (0)|rust:latest\n"
        result = _parse_docker_ps(raw)
        assert len(result) == 2
        assert result[0]["name"] == "mc_server"
        assert result[0]["status"] == "Up 2 days"
        assert result[1]["image"] == "rust:latest"

    def test_parse_docker_ps_empty(self):
        assert _parse_docker_ps("") == []
        assert _parse_docker_ps("   ") == []

    def test_parse_docker_ps_malformed_lines(self):
        raw = "nodelimiter\nvalid|Up 1 day|image:tag\n"
        result = _parse_docker_ps(raw)
        assert len(result) == 1
        assert result[0]["name"] == "valid"

    def test_classify_status(self):
        assert _classify_status("Up 2 days") == "running"
        assert _classify_status("Exited (0) 3 hours ago") == "stopped"
        assert _classify_status("Restarting (1) 5 seconds ago") == "errored"

    def test_recently_restarted(self):
        assert _recently_restarted("Up 5 minutes") is True
        assert _recently_restarted("Up 30 seconds") is True
        assert _recently_restarted("Up About an hour") is True
        assert _recently_restarted("Up 3 hours") is False
        assert _recently_restarted("Exited (0) 2 minutes ago") is False

    def test_parse_docker_stats(self):
        raw = "mc_srv\t12.5%\t512MiB / 2GiB\t25.0%\nrust_srv\t3.2%\t1GiB / 4GiB\t25.0%\n"
        result = _parse_docker_stats(raw)
        assert len(result) == 2
        assert result[0]["name"] == "mc_srv"
        assert result[0]["cpu"] == "12.5%"

    def test_parse_disk_usage(self):
        raw = "Filesystem      Size  Used Avail Use% Mounted on\n/dev/sda1       100G   45G   55G  45% /\n"
        result = _parse_disk_usage(raw)
        assert result is not None
        assert result["use_pct"] == "45"
        assert result["avail"] == "55G"

    def test_parse_disk_usage_empty(self):
        assert _parse_disk_usage("") is None

    def test_pct_value(self):
        assert _pct_value("45.2%") == 45.2
        assert _pct_value("100%") == 100.0
        assert _pct_value("nope") == 0.0


class TestBuildOverviewReport:
    def _node_checks(self, **overrides):
        checks = {
            "wings_service": _ok("active"),
            "wings_config": _ok("port: 8080"),
            "disk": _ok("Filesystem      Size  Used Avail Use% Mounted on\n/dev/sda1       200G   60G  140G  30% /srv/pterodactyl\n"),
            "docker_ps": _ok("mc_01|Up 3 days|itzg/minecraft\nmc_02|Up 1 day|itzg/minecraft\n"),
            "docker_stats": _ok("mc_01\t5.0%\t256MiB / 2GiB\t12.5%\nmc_02\t8.0%\t1GiB / 4GiB\t25.0%\n"),
            "restarting": _ok(""),
        }
        checks.update(overrides)
        return checks

    def test_healthy_fleet(self):
        results = {"node-01": self._node_checks()}
        report = _build_overview_report(results)
        assert "PTERODACTYL FLEET OVERVIEW" in report
        assert "Nodes scanned: 1" in report
        assert "2 total" in report
        assert "2 running" in report
        assert "No problems detected" in report

    def test_wings_down_flagged(self):
        results = {"node-01": self._node_checks(wings_service=_ok("inactive"))}
        report = _build_overview_report(results)
        assert "WINGS SERVICE DOWN" in report
        assert "node-01 (inactive)" in report

    def test_disk_critical_flagged(self):
        disk_output = "Filesystem  Size  Used Avail Use% Mounted on\n/dev/sda1  200G  180G  20G  90% /srv\n"
        results = {"node-01": self._node_checks(disk=_ok(disk_output))}
        report = _build_overview_report(results)
        assert "DISK SPACE CRITICAL" in report
        assert "** CRITICAL **" in report

    def test_restart_loop_flagged(self):
        results = {"node-01": self._node_checks(
            restarting=_ok("bad_srv|Restarting (1) 5 seconds ago|some:image\n")
        )}
        report = _build_overview_report(results)
        assert "CONTAINERS IN RESTART LOOP" in report
        assert "bad_srv" in report

    def test_high_memory_flagged(self):
        stats = "mc_hot\t50.0%\t3.8GiB / 4GiB\t95.0%\n"
        results = {"node-01": self._node_checks(docker_stats=_ok(stats))}
        report = _build_overview_report(results)
        assert "HIGH MEMORY USAGE" in report
        assert "mc_hot" in report

    def test_recently_restarted_flagged(self):
        ps = "mc_new|Up 5 minutes|itzg/minecraft\n"
        results = {"node-01": self._node_checks(docker_ps=_ok(ps))}
        report = _build_overview_report(results)
        assert "RECENTLY RESTARTED" in report

    def test_multi_node_aggregation(self):
        results = {
            "node-01": self._node_checks(),
            "node-02": self._node_checks(),
        }
        report = _build_overview_report(results)
        assert "Nodes scanned: 2" in report
        assert "Total containers: 4" in report

    def test_errored_node(self):
        bad_checks = {
            k: _err() for k in ["wings_service", "wings_config", "disk", "docker_ps", "docker_stats", "restarting"]
        }
        results = {"broken-node": bad_checks}
        report = _build_overview_report(results)
        assert "broken-node" in report
        assert "Wings service: unknown" in report
