"""Tests for wp_deep_scan helper functions."""

from __future__ import annotations

from agent.tools.base import ToolResult
from agent.tools.wp_deep_scan import _build_wp_report


def _ok(output: str = "") -> ToolResult:
    return ToolResult(output=output)


def _err() -> ToolResult:
    return ToolResult(error="fail", exit_code=1)


class TestBuildWpReport:
    def _base_data(self, **overrides):
        data = {
            "ttfb": _ok("dns:0.001|connect:0.010|ttfb:0.250|total:0.300|size:25000|code:200"),
            "autoload_size": _ok("500000"),
            "autoload_top": _ok("option_one\t200000\noption_two\t100000\n"),
            "object_cache": _ok("-rw-r--r-- 1 user user 5000 object-cache.php"),
            "cache_type": _ok("<?php\n// Redis Object Cache"),
            "opcache": _ok("hit_rate:98.5\ncached:350\nmemory_used:50.2MB"),
            "plugins": _ok("12"),
            "page_cache": _ok("litespeed-cache,active,none,5.0\n"),
            "db_size": _ok("Name,Size\nwp_posts,15MB\nwp_options,2MB\n"),
            "bloat": _ok("transients\trevisions\tspam\ttrash\n50\t200\t10\t5\n"),
            "php_config": _ok("memory_limit:256M\nmax_execution_time:30"),
            "cron_constant": _ok("1"),
            "large_images": _ok("3"),
            "uploads_size": _ok("500M\t/home/user/public_html/wp-content/uploads"),
            "headers": _ok("Content-Encoding: gzip\nCache-Control: max-age=3600"),
            "wp_http": _ok("25"),
        }
        data.update(overrides)
        return data

    def test_healthy_site(self):
        report = _build_wp_report("example.com", "/home/user/public_html", self._base_data())
        assert "example.com" in report
        assert "TTFB excellent" in report
        assert "Redis object cache" in report
        assert "litespeed-cache" in report

    def test_slow_ttfb(self):
        report = _build_wp_report("slow.com", "/home/u/public_html", self._base_data(
            ttfb=_ok("dns:0.001|connect:0.010|ttfb:3.500|total:4.000|size:25000|code:200"),
        ))
        assert "TTFB 3.50s" in report
        assert "extremely slow" in report

    def test_autoload_bloat(self):
        report = _build_wp_report("bloated.com", "/home/u/public_html", self._base_data(
            autoload_size=_ok("3145728"),  # 3MB
        ))
        assert "AUTOLOAD BLOAT" in report
        assert "3.0 MB" in report

    def test_no_object_cache(self):
        report = _build_wp_report("nocache.com", "/home/u/public_html", self._base_data(
            object_cache=_err(),
            cache_type=_err(),
        ))
        assert "NO OBJECT CACHE" in report

    def test_opcache_disabled(self):
        report = _build_wp_report("noopcache.com", "/home/u/public_html", self._base_data(
            opcache=_ok("OPcache not available"),
        ))
        assert "OPCACHE DISABLED" in report

    def test_low_opcache_hit_rate(self):
        report = _build_wp_report("low.com", "/home/u/public_html", self._base_data(
            opcache=_ok("hit_rate:75.0\ncached:100\nmemory_used:20MB"),
        ))
        assert "hit rate 75%" in report

    def test_wp_cron_on_page_loads(self):
        report = _build_wp_report("cron.com", "/home/u/public_html", self._base_data(
            cron_constant=_ok("0"),
        ))
        assert "WP-CRON ON PAGE LOADS" in report

    def test_too_many_plugins(self):
        report = _build_wp_report("plugins.com", "/home/u/public_html", self._base_data(
            plugins=_ok("45"),
        ))
        assert "45 active plugins" in report

    def test_no_compression(self):
        report = _build_wp_report("nocomp.com", "/home/u/public_html", self._base_data(
            headers=_ok("Content-Type: text/html"),
        ))
        assert "compression" in report.lower()
