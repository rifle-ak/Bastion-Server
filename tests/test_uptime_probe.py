"""Tests for uptime_probe helper functions."""

from __future__ import annotations

from agent.tools.uptime_probe import _format_probes


class TestFormatProbes:
    def test_healthy_endpoints(self):
        results = {
            "https://example.com": {
                "url": "https://example.com",
                "code": "200",
                "ttfb": "0.150",
                "total": "0.300",
                "size": "25000",
                "redirect": "0",
            },
        }
        report = _format_probes(results)
        assert "✓" in report
        assert "200" in report
        assert "All endpoints healthy" in report

    def test_server_error(self):
        results = {
            "https://broken.com": {
                "url": "https://broken.com",
                "code": "500",
                "ttfb": "0.050",
                "total": "0.100",
                "size": "500",
                "redirect": "0",
            },
        }
        report = _format_probes(results)
        assert "✗" in report
        assert "500" in report
        assert "issues" in report

    def test_404_warning(self):
        results = {
            "https://missing.com/page": {
                "url": "https://missing.com/page",
                "code": "404",
                "ttfb": "0.100",
                "total": "0.200",
                "size": "1000",
                "redirect": "0",
            },
        }
        report = _format_probes(results)
        assert "⚠" in report
        assert "404" in report

    def test_connection_error(self):
        results = {
            "https://down.com": {
                "url": "https://down.com",
                "error": "Connection timed out",
            },
        }
        report = _format_probes(results)
        assert "✗" in report
        assert "Connection timed out" in report

    def test_slow_ttfb(self):
        results = {
            "https://slow.com": {
                "url": "https://slow.com",
                "code": "200",
                "ttfb": "3.500",
                "total": "5.000",
                "size": "50000",
                "redirect": "0",
            },
        }
        report = _format_probes(results)
        assert "TTFB very slow" in report

    def test_redirects_shown(self):
        results = {
            "http://redirect.com": {
                "url": "http://redirect.com",
                "code": "200",
                "ttfb": "0.200",
                "total": "0.400",
                "size": "10000",
                "redirect": "2",
            },
        }
        report = _format_probes(results)
        assert "Redirects: 2" in report

    def test_ssl_expiry_shown(self):
        results = {
            "https://ssl.com": {
                "url": "https://ssl.com",
                "code": "200",
                "ttfb": "0.100",
                "total": "0.200",
                "size": "5000",
                "redirect": "0",
                "ssl_expiry": "notAfter=Dec 31 2026",
            },
        }
        report = _format_probes(results)
        assert "notAfter" in report

    def test_content_match_pass(self):
        results = {
            "https://test.com": {
                "url": "https://test.com",
                "code": "200",
                "ttfb": "0.100",
                "total": "0.200",
                "size": "5000",
                "redirect": "0",
                "content_match": True,
            },
        }
        report = _format_probes(results)
        assert "Content match: found" in report

    def test_content_match_fail(self):
        results = {
            "https://test.com": {
                "url": "https://test.com",
                "code": "200",
                "ttfb": "0.100",
                "total": "0.200",
                "size": "5000",
                "redirect": "0",
                "content_match": False,
            },
        }
        report = _format_probes(results)
        assert "NOT FOUND" in report

    def test_multiple_urls(self):
        results = {
            "https://ok.com": {
                "code": "200", "ttfb": "0.1", "total": "0.2",
                "size": "1000", "redirect": "0",
            },
            "https://bad.com": {
                "error": "Connection refused",
            },
        }
        report = _format_probes(results)
        assert "ok.com" in report
        assert "bad.com" in report
        assert "issues" in report
