"""Tests for cPanel, WordPress, web server, and database tool formatters."""

from __future__ import annotations

import json

from agent.tools.cpanel import _format_accounts, _format_ssl
from agent.tools.webserver import _summarize_errors
from agent.tools.database import _extract_mysql_metrics


class TestCpanelFormatAccounts:
    def test_normal_accounts(self):
        data = {
            "data": {
                "acct": [
                    {"user": "john", "domain": "example.com", "plan": "basic", "diskused": "500", "suspended": 0},
                    {"user": "jane", "domain": "test.org", "plan": "pro", "diskused": "1200", "suspended": 1},
                ]
            }
        }
        result = _format_accounts(json.dumps(data))
        assert "john" in result
        assert "example.com" in result
        assert "YES" in result  # jane is suspended
        assert "jane" in result

    def test_no_accounts(self):
        data = {"data": {"acct": []}}
        result = _format_accounts(json.dumps(data))
        assert "No accounts" in result

    def test_invalid_json(self):
        result = _format_accounts("not json")
        assert result == "not json"


class TestCpanelFormatSSL:
    def test_no_problems(self):
        data = {"data": {"problems": []}}
        result = _format_ssl(json.dumps(data))
        assert "No problems" in result

    def test_ssl_problems(self):
        data = {
            "data": {
                "problems": [
                    {"domain": "bad.com", "problem": "Certificate expired"},
                    {"domain": "worse.com", "problem": "No certificate installed"},
                ]
            }
        }
        result = _format_ssl(json.dumps(data))
        assert "bad.com" in result
        assert "expired" in result
        assert "worse.com" in result


class TestWebErrorSummarizer:
    def test_no_errors(self):
        result = _summarize_errors("", None)
        assert "No errors" in result

    def test_php_fatal_grouped(self):
        log = "\n".join([
            "[Mon Mar 14] PHP Fatal error: foo in /var/www/test.php",
            "[Mon Mar 14] PHP Fatal error: bar in /var/www/test2.php",
            "[Mon Mar 14] PHP Warning: something in /var/www/test3.php",
            "[Mon Mar 14] normal access log line",
        ])
        result = _summarize_errors(log, None)
        assert "PHP Fatal" in result
        assert "PHP Warning" in result

    def test_domain_filter(self):
        log = "\n".join([
            "[Mon] error for example.com blah",
            "[Mon] error for other.com blah",
        ])
        result = _summarize_errors(log, "example.com")
        assert "1 lines" in result

    def test_permission_denied(self):
        log = "[Mon] Permission denied: /var/www/secret\n" * 15
        result = _summarize_errors(log, None)
        assert "Permission denied" in result
        assert "15" in result

    def test_modsecurity(self):
        log = "[Mon] ModSecurity: Access denied with code 403\n"
        result = _summarize_errors(log, None)
        assert "ModSecurity" in result


class TestMySQLMetrics:
    def test_extract_key_vars(self):
        output = (
            "| Threads_connected      | 5     |\n"
            "| Threads_running        | 2     |\n"
            "| Slow_queries           | 42    |\n"
            "| Random_other_var       | 999   |\n"
        )
        metrics = _extract_mysql_metrics(output)
        assert any("Connected threads" in m for m in metrics)
        assert any("Running threads" in m for m in metrics)
        assert any("Slow queries" in m for m in metrics)
        # Random_other_var should not appear
        assert not any("999" in m for m in metrics)

    def test_empty_output(self):
        assert _extract_mysql_metrics("") == []
