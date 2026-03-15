"""Tests for customer_impact report builder and helpers."""

from __future__ import annotations

from agent.tools.base import ToolResult
from agent.tools.customer_impact import (
    ImpactData,
    _assess_revenue_risk,
    _build_impact_report,
    _communication_recommendations,
    _merge_impact_data,
    _recovery_priority,
    _risk_reasoning,
)


def _ok(output: str = "") -> ToolResult:
    return ToolResult(output=output)


def _err() -> ToolResult:
    return ToolResult(error="fail", exit_code=1)


class TestBuildImpactReport:
    def test_full_report_with_accounts(self):
        data = ImpactData(
            account_count=5,
            accounts=["user1", "user2", "user3", "user4", "user5"],
            domain_map={"example.com": "user1", "test.org": "user2"},
            domain_count=2,
            database_count=10,
            container_count=0,
        )
        report = _build_impact_report("web-01", "apache", data)
        assert "Impact Summary" in report
        assert "5 customer account(s)" in report
        assert "2 website(s)/domain(s)" in report
        assert "10 database(s)" in report
        assert "apache" in report
        assert "web-01" in report

    def test_report_with_containers_only(self):
        data = ImpactData(
            container_count=8,
            container_names=["gs1", "gs2", "gs3", "gs4", "gs5", "gs6", "gs7", "gs8"],
        )
        report = _build_impact_report("game-01", "wings", data)
        assert "8 container(s)/game server(s)" in report
        assert "Affected Containers" in report
        assert "gs1" in report

    def test_report_no_data(self):
        data = ImpactData()
        report = _build_impact_report("srv", "php-fpm", data)
        assert "Unable to determine exact count" in report

    def test_report_includes_errors(self):
        data = ImpactData(errors=["Connection timed out", "Permission denied"])
        report = _build_impact_report("srv", "mysql", data)
        assert "Data Collection Notes" in report
        assert "Connection timed out" in report

    def test_report_with_domain_map_groups_by_account(self):
        data = ImpactData(
            account_count=2,
            accounts=["alice", "bob"],
            domain_map={"a.com": "alice", "b.com": "alice", "c.com": "bob"},
            domain_count=3,
        )
        report = _build_impact_report("web-01", "apache", data)
        assert "**alice**: 2 domain(s)" in report
        assert "**bob**: 1 domain(s)" in report

    def test_report_customer_facing_server_outage(self):
        data = ImpactData(account_count=30)
        report = _build_impact_report("web-01", "server", data)
        assert "Recovery Priority" in report
        assert "Restore network" in report
        assert "Revenue Risk" in report
        assert "HIGH" in report

    def test_report_no_customer_communication_for_unknown_service(self):
        data = ImpactData()
        report = _build_impact_report("srv", "named", data)
        assert "DNS resolution" in report


class TestAssessRevenueRisk:
    def test_server_outage_always_high(self):
        assert _assess_revenue_risk("server", ImpactData()) == "HIGH"

    def test_many_accounts_high(self):
        assert _assess_revenue_risk("apache", ImpactData(account_count=25)) == "HIGH"

    def test_many_containers_high(self):
        assert _assess_revenue_risk("docker", ImpactData(container_count=12)) == "HIGH"

    def test_moderate_accounts_medium(self):
        assert _assess_revenue_risk("apache", ImpactData(account_count=7)) == "MEDIUM"

    def test_core_service_low_counts_medium(self):
        assert _assess_revenue_risk("mysql", ImpactData(account_count=1)) == "MEDIUM"

    def test_non_core_low_counts_low(self):
        assert _assess_revenue_risk("exim", ImpactData(account_count=1)) == "LOW"


class TestMergeImpactData:
    def test_merge_takes_higher_counts(self):
        base = ImpactData(account_count=5, database_count=3)
        other = ImpactData(account_count=10, database_count=1)
        merged = _merge_impact_data(base, other)
        assert merged.account_count == 10
        assert merged.database_count == 3

    def test_merge_combines_containers(self):
        base = ImpactData(container_names=["a", "b"])
        other = ImpactData(container_names=["c"])
        merged = _merge_impact_data(base, other)
        assert merged.container_names == ["a", "b", "c"]

    def test_merge_combines_errors(self):
        base = ImpactData(errors=["err1"])
        other = ImpactData(errors=["err2"])
        merged = _merge_impact_data(base, other)
        assert merged.errors == ["err1", "err2"]


class TestRecoveryPriority:
    def test_known_service_returns_steps(self):
        steps = _recovery_priority("mysql")
        assert len(steps) >= 3
        assert any("MySQL" in s or "mysql" in s for s in steps)

    def test_unknown_service_returns_generic(self):
        steps = _recovery_priority("unknownsvc")
        assert len(steps) == 1
        assert "unknownsvc" in steps[0]
