"""Tests for resource_rightsizing report builder and parsing helpers."""

from __future__ import annotations

from agent.tools.base import ToolResult
from agent.tools.resource_rightsizing import (
    _build_container_section,
    _build_host_section,
    _build_quick_wins,
    _build_recommendations,
    _build_rightsizing_report,
    _classify_usage,
    _format_bytes,
    _parse_cpu_count,
    _parse_df_output,
    _parse_docker_stats,
    _parse_free_output,
    _parse_loadavg,
    _parse_memory_bytes,
    _parse_percentage,
)


def _ok(output: str = "") -> ToolResult:
    return ToolResult(output=output)


def _err() -> ToolResult:
    return ToolResult(error="fail", exit_code=1)


class TestParsingHelpers:
    def test_parse_percentage(self):
        assert _parse_percentage("45.2%") == 45.2
        assert _parse_percentage("100%") == 100.0
        assert _parse_percentage("0%") == 0.0
        assert _parse_percentage("bad") is None

    def test_parse_memory_bytes(self):
        assert _parse_memory_bytes("1GiB") == 1024**3
        assert _parse_memory_bytes("512MiB") == 512 * 1024**2
        assert _parse_memory_bytes("1GB") == 1000**3
        assert _parse_memory_bytes("invalid") is None

    def test_format_bytes(self):
        assert "GiB" in _format_bytes(1024**3)
        assert "MiB" in _format_bytes(1024**2)
        assert "KiB" in _format_bytes(1024)

    def test_classify_usage(self):
        assert _classify_usage(10.0) == "over-provisioned"
        assert _classify_usage(50.0) == "right-sized"
        assert _classify_usage(90.0) == "under-provisioned"
        assert _classify_usage(30.0) == "right-sized"
        assert _classify_usage(85.0) == "right-sized"

    def test_parse_free_output(self):
        output = (
            "              total        used        free\n"
            "Mem:    8589934592  4294967296  4294967296\n"
            "Swap:   2147483648   104857600  2042626048\n"
        )
        result = _parse_free_output(output)
        assert result["ram_total"] == 8589934592
        assert result["ram_used"] == 4294967296
        assert result["ram_pct"] == 50.0
        assert result["swap_used"] == 104857600

    def test_parse_df_output(self):
        output = (
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "/dev/sda1       100G   45G   55G  45% /\n"
            "/dev/sdb1       500G  400G  100G  80% /srv\n"
        )
        result = _parse_df_output(output)
        assert len(result) == 2
        assert result[0]["use_pct"] == 45.0
        assert result[1]["mount"] == "/srv"

    def test_parse_loadavg(self):
        output = " 12:00:00 up 30 days,  1 user,  load average: 2.50, 1.80, 1.20"
        loads = _parse_loadavg(output)
        assert loads == [2.5, 1.8, 1.2]

    def test_parse_loadavg_no_data(self):
        assert _parse_loadavg("no load info here") == []

    def test_parse_cpu_count(self):
        assert _parse_cpu_count("8") == 8
        assert _parse_cpu_count("bad") is None

    def test_parse_docker_stats(self):
        output = "mc_srv\t256MiB / 2GiB\t12.5%\t5.0%\n"
        result = _parse_docker_stats(output)
        assert len(result) == 1
        assert result[0]["name"] == "mc_srv"
        assert result[0]["mem_pct"] == 12.5
        assert result[0]["cpu_pct"] == 5.0
        assert result[0]["mem_used"] == 256 * 1024**2


class TestBuildContainerSection:
    def test_classifies_containers(self):
        containers = [
            {"name": "idle", "mem_pct": 10.0, "cpu_pct": 1.0, "mem_used": None, "mem_limit": None},
            {"name": "busy", "mem_pct": 90.0, "cpu_pct": 80.0, "mem_used": None, "mem_limit": None},
            {"name": "ok", "mem_pct": 50.0, "cpu_pct": 30.0, "mem_used": None, "mem_limit": None},
        ]
        result = _build_container_section(containers)
        assert len(result["over"]) == 1
        assert "idle" in result["over"][0]
        assert len(result["under"]) == 1
        assert "busy" in result["under"][0]
        assert len(result["right"]) == 1

    def test_skips_containers_without_mem_pct(self):
        containers = [{"name": "unknown", "mem_pct": None, "cpu_pct": None}]
        result = _build_container_section(containers)
        assert result == {"over": [], "under": [], "right": []}


class TestBuildRightsizingReport:
    def test_minimal_report(self):
        report = _build_rightsizing_report("srv-01", {})
        assert "Resource Rightsizing Report: srv-01" in report
        assert "No host-level data available" in report
        assert "Recommendations" in report

    def test_report_with_host_data(self):
        data = {
            "memory": {"ram_total": 8589934592, "ram_used": 4294967296, "ram_pct": 50.0},
            "load_avg": [1.0, 0.8, 0.5],
            "cpu_count": 4,
            "disks": [{"filesystem": "/dev/sda1", "size": "100G", "used": "45G",
                        "available": "55G", "use_pct": 45.0, "mount": "/"}],
        }
        report = _build_rightsizing_report("srv-01", data)
        assert "50.0% used" in report
        assert "right-sized" in report

    def test_report_with_over_provisioned_containers(self):
        data = {
            "containers": [
                {"name": "idle_srv", "mem_pct": 5.0, "cpu_pct": 0.5,
                 "mem_used": 50 * 1024**2, "mem_limit": 2 * 1024**3},
            ],
        }
        report = _build_rightsizing_report("srv-01", data)
        assert "Over-provisioned" in report
        assert "idle_srv" in report

    def test_report_recommendations_for_high_ram(self):
        data = {"memory": {"ram_total": 8589934592, "ram_used": 7730941132, "ram_pct": 90.0}}
        report = _build_rightsizing_report("srv-01", data)
        assert "Upgrade RAM urgently" in report

    def test_report_quick_wins_low_cpu(self):
        data = {
            "load_avg": [0.1, 0.05, 0.02],
            "cpu_count": 8,
            "memory": {"ram_total": 1, "ram_used": 0, "ram_pct": 15.0},
        }
        report = _build_rightsizing_report("srv-01", data)
        assert "Quick Wins" in report
        assert "CPU nearly idle" in report

    def test_report_swap_usage_warning(self):
        data = {"memory": {"ram_total": 8589934592, "ram_used": 4294967296,
                            "ram_pct": 50.0, "swap_total": 2147483648, "swap_used": 100000000}}
        report = _build_rightsizing_report("srv-01", data)
        assert "swap" in report.lower()
        assert "Upgrade RAM" in report
