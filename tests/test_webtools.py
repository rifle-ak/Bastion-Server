"""Tests for cPanel, WordPress, web server, and database tool formatters."""

from __future__ import annotations

import json

from agent.tools.cpanel import _format_accounts, _format_ssl
from agent.tools.webserver import (
    _analyze_access_log,
    _analyze_modsec,
    _summarize_errors,
)
from agent.tools.wordpress import _format_security_scan
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
        assert "YES" in result
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


class TestAccessLogAnalysis:
    def test_basic_analysis(self):
        log = "\n".join([
            '192.168.1.1 - - [14/Mar/2026:10:00:00] "GET / HTTP/1.1" 200 1234',
            '192.168.1.1 - - [14/Mar/2026:10:00:01] "GET /page HTTP/1.1" 200 5678',
            '10.0.0.1 - - [14/Mar/2026:10:00:02] "GET /other HTTP/1.1" 404 0',
        ])
        result = _analyze_access_log(log)
        assert "3 requests" in result
        assert "192.168.1.1" in result
        assert "200" in result
        assert "404" in result

    def test_brute_force_detection(self):
        lines = []
        for i in range(30):
            lines.append(f'10.0.0.99 - - [14/Mar/2026:10:{i:02d}:00] "POST /wp-login.php HTTP/1.1" 200 0')
        result = _analyze_access_log("\n".join(lines))
        assert "brute force" in result.lower()
        assert "10.0.0.99" in result

    def test_empty_log(self):
        result = _analyze_access_log("")
        assert "No log entries" in result

    def test_status_code_breakdown(self):
        lines = []
        for _ in range(10):
            lines.append('1.1.1.1 - - [14/Mar/2026:10:00:00] "GET / HTTP/1.1" 500 0')
        result = _analyze_access_log("\n".join(lines))
        assert "500" in result
        assert "✗" in result  # 500s get error icon


class TestModSecurityAnalysis:
    def test_no_modsec_entries(self):
        result = _analyze_modsec("some normal log line\nanother line", None)
        assert "No ModSecurity" in result

    def test_rule_id_extraction(self):
        log = (
            '[Mon] [client 10.0.0.1] ModSecurity: Access denied [id "12345"] '
            '[uri "/wp-admin/upload.php"] [msg "blocked"]\n'
            '[Mon] [client 10.0.0.2] ModSecurity: Access denied [id "12345"] '
            '[uri "/wp-login.php"] [msg "blocked"]\n'
            '[Mon] [client 10.0.0.1] ModSecurity: Access denied [id "67890"] '
            '[uri "/xmlrpc.php"] [msg "blocked"]\n'
        )
        result = _analyze_modsec(log, None)
        assert "12345" in result
        assert "67890" in result
        assert "3 entries" in result

    def test_domain_filter(self):
        log = (
            '[Mon] [client 10.0.0.1] ModSecurity: blocked for example.com [id "111"]\n'
            '[Mon] [client 10.0.0.1] ModSecurity: blocked for other.com [id "222"]\n'
        )
        result = _analyze_modsec(log, "example.com")
        assert "1 entries" in result

    def test_ip_extraction(self):
        log = '[Mon] [client 192.168.1.100] ModSecurity: Access denied [id "999"]\n'
        result = _analyze_modsec(log, None)
        assert "192.168.1.100" in result


class TestSecurityScanFormatter:
    def test_all_clear(self):
        results = {
            "core_verify": "Success: WordPress installation verifies against checksums.",
            "php_in_uploads": "",
            "world_writable": "",
            "obfuscated": "",
            "htaccess": "",
            "recent_core_changes": "",
        }
        output = _format_security_scan(results)
        assert "All clear" in output
        assert "✓" in output

    def test_php_in_uploads_detected(self):
        results = {
            "core_verify": "Success",
            "php_in_uploads": "/home/user/public_html/wp-content/uploads/shell.php\n"
                              "/home/user/public_html/wp-content/uploads/2024/hack.php",
            "world_writable": "",
            "obfuscated": "",
            "htaccess": "",
            "recent_core_changes": "",
        }
        output = _format_security_scan(results)
        assert "2 found" in output
        assert "malware" in output.lower()
        assert "shell.php" in output

    def test_obfuscated_code_detected(self):
        results = {
            "core_verify": "Success",
            "php_in_uploads": "",
            "world_writable": "",
            "obfuscated": "/home/user/public_html/wp-content/plugins/bad/evil.php",
            "htaccess": "",
            "recent_core_changes": "",
        }
        output = _format_security_scan(results)
        assert "Obfuscated" in output
        assert "evil.php" in output

    def test_core_integrity_failed(self):
        results = {
            "core_verify": "Warning! File doesn't verify against its checksum.\nwp-includes/version.php",
            "php_in_uploads": "",
            "world_writable": "",
            "obfuscated": "",
            "htaccess": "",
            "recent_core_changes": "",
        }
        output = _format_security_scan(results)
        assert "MODIFIED" in output
        assert "version.php" in output


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
        assert not any("999" in m for m in metrics)

    def test_empty_output(self):
        assert _extract_mysql_metrics("") == []
