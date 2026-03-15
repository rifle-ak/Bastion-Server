"""Tests for the diagnose_site tool helpers."""

from __future__ import annotations

from agent.tools.diagnose import _extract_owner, _build_report
from agent.tools.base import ToolResult


class TestExtractOwner:
    def test_valid_json(self):
        raw = '{"data": {"user": "johndoe"}}'
        assert _extract_owner(raw) == "johndoe"

    def test_no_user(self):
        raw = '{"data": {}}'
        assert _extract_owner(raw) is None

    def test_invalid_json(self):
        assert _extract_owner("not json") is None

    def test_empty_string(self):
        assert _extract_owner("") is None


class TestBuildReport:
    def _make_result(self, output="", error="", exit_code=0):
        return ToolResult(output=output, error=error, exit_code=exit_code)

    def test_basic_report_structure(self):
        checks = {
            "dns_a": self._make_result("1.2.3.4"),
            "dns_mx": self._make_result("10 mail.example.com."),
            "dns_ns": self._make_result("ns1.example.com."),
            "ssl": self._make_result("notBefore=Mar 1\nnotAfter=Jun 1"),
        }
        report = _build_report(
            "example.com", "testuser", "/home/testuser/public_html",
            checks, False, {}, "",
        )
        assert "example.com" in report
        assert "testuser" in report
        assert "1.2.3.4" in report
        assert "DNS" in report
        assert "SSL" in report

    def test_wordpress_detected(self):
        checks = {
            "dns_a": self._make_result("1.2.3.4"),
            "dns_mx": self._make_result(""),
            "dns_ns": self._make_result(""),
            "ssl": self._make_result(""),
        }
        wp_results = {
            "wp_version": self._make_result("6.4.2"),
            "wp_core_verify": self._make_result("Success: WordPress installation verifies."),
            "wp_db_size": self._make_result("15 MB"),
            "wp_plugins": self._make_result("name,status,update,version\nakismet,active,available,5.0"),
            "php_uploads": self._make_result(""),
        }
        report = _build_report(
            "example.com", "testuser", "/home/testuser/public_html",
            checks, True, wp_results, "",
        )
        assert "WordPress" in report
        assert "6.4.2" in report
        assert "Verified" in report
        assert "need updates" in report

    def test_malware_detected(self):
        checks = {
            "dns_a": self._make_result("1.2.3.4"),
            "dns_mx": self._make_result(""),
            "dns_ns": self._make_result(""),
            "ssl": self._make_result(""),
        }
        wp_results = {
            "wp_version": self._make_result("6.4"),
            "wp_core_verify": self._make_result("Warning! Modified"),
            "wp_db_size": self._make_result("10 MB"),
            "wp_plugins": self._make_result("name,status\nplugin1,active"),
            "php_uploads": self._make_result("/uploads/shell.php\n/uploads/hack.php"),
        }
        report = _build_report(
            "bad.com", "hacked", "/home/hacked/public_html",
            checks, True, wp_results, "",
        )
        assert "malware" in report.lower()
        assert "2 found" in report or "MODIFIED" in report

    def test_error_log_included(self):
        checks = {
            "dns_a": self._make_result("1.2.3.4"),
            "dns_mx": self._make_result(""),
            "dns_ns": self._make_result(""),
            "ssl": self._make_result(""),
        }
        error_log = "[Mon] PHP Fatal error: something in /var/www/test.php"
        report = _build_report(
            "example.com", "testuser", "/home/testuser/public_html",
            checks, False, {}, error_log,
        )
        assert "PHP Fatal" in report
        assert "error" in report.lower()

    def test_no_account_owner(self):
        checks = {
            "dns_a": self._make_result("1.2.3.4"),
            "dns_mx": self._make_result(""),
            "dns_ns": self._make_result(""),
            "ssl": self._make_result(""),
        }
        report = _build_report(
            "unknown.com", None, None, checks, False, {}, "",
        )
        assert "Could not identify" in report

    def test_clean_site(self):
        checks = {
            "dns_a": self._make_result("1.2.3.4"),
            "dns_mx": self._make_result("10 mail.test.com."),
            "dns_ns": self._make_result("ns1.test.com."),
            "ssl": self._make_result("subject=test.com\nnotAfter=Dec 31 2026"),
            "disk_summary": self._make_result("250M\t/home/clean/"),
            "disk_breakdown": self._make_result("100M\t/home/clean/public_html/"),
            "mail_queue": self._make_result("5"),
            "mail_boxes": self._make_result("1M\t/home/clean/mail/test.com/"),
        }
        report = _build_report(
            "test.com", "clean", "/home/clean/public_html",
            checks, False, {}, "",
        )
        assert "No recent errors" in report
        assert "250M" in report
