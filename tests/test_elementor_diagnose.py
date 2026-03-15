"""Tests for Elementor rendering diagnostics."""

from __future__ import annotations

import json

import pytest

from agent.tools.wp_elementor_diagnose import (
    _analyze_elementor_page,
    _build_elementor_report,
    _check_elementor_data,
    _check_plugin_conflicts,
    _check_shortcode,
    _check_widget_html,
    _major_minor,
)
from agent.tools.base import ToolResult


# ── Version helpers ──


class TestMajorMinor:
    def test_normal_version(self):
        assert _major_minor("3.18.2") == "3.18"

    def test_two_part(self):
        assert _major_minor("3.18") == "3.18"

    def test_single_part(self):
        assert _major_minor("3") == ""

    def test_empty(self):
        assert _major_minor("") == ""


# ── Widget HTML checks ──


class TestCheckWidgetHtml:
    def test_unclosed_script(self):
        html = '<script>var x = 1;'
        issues = _check_widget_html(html)
        assert any("UNCLOSED <script>" in i for i in issues)

    def test_closed_script_ok(self):
        html = '<script>var x = 1;</script>'
        issues = _check_widget_html(html)
        assert not any("UNCLOSED <script>" in i for i in issues)

    def test_unclosed_style(self):
        html = '<style>.foo { color: red; }'
        issues = _check_widget_html(html)
        assert any("UNCLOSED <style>" in i for i in issues)

    def test_unclosed_iframe(self):
        html = '<iframe src="https://example.com">'
        issues = _check_widget_html(html)
        assert any("UNCLOSED <iframe>" in i for i in issues)

    def test_unclosed_divs(self):
        html = '<div class="a"><div class="b"><div class="c">content</div>'
        issues = _check_widget_html(html)
        # 3 opens, 1 close -> diff of 2, threshold is >2
        assert not issues  # Exactly at threshold, not over

    def test_many_unclosed_divs(self):
        html = '<div><div><div><div>content</div>'
        issues = _check_widget_html(html)
        assert any("unclosed <div>" in i for i in issues)

    def test_raw_php(self):
        html = '<?php echo "hello"; ?>'
        issues = _check_widget_html(html)
        assert any("RAW PHP" in i for i in issues)

    def test_clean_html(self):
        html = '<div class="wrapper"><p>Hello world</p></div>'
        issues = _check_widget_html(html)
        assert issues == []


# ── Shortcode checks ──


class TestCheckShortcode:
    def test_balanced(self):
        sc = '[my_shortcode param="value"]'
        assert _check_shortcode(sc) == []

    def test_unbalanced_brackets(self):
        sc = '[my_shortcode param="value"'
        issues = _check_shortcode(sc)
        assert any("MALFORMED SHORTCODE" in i for i in issues)

    def test_deeply_nested(self):
        sc = '[outer][inner][deeper][deepest]content[/deepest][/deeper][/inner][/outer]'
        issues = _check_shortcode(sc)
        assert any("nested shortcodes" in i.lower() for i in issues)


# ── Plugin conflicts ──


class TestCheckPluginConflicts:
    def test_no_conflicts(self):
        csv = "name,version\nwoocommerce,8.5.1\nelementor,3.18.2\n"
        assert _check_plugin_conflicts(csv) == []

    def test_autoptimize_conflict(self):
        csv = "name,version\nautoptimize,3.1.0\nelementor,3.18.2\n"
        issues = _check_plugin_conflicts(csv)
        assert len(issues) == 1
        assert "autoptimize" in issues[0].lower()

    def test_multiple_conflicts(self):
        csv = "name,version\nautoptimize,3.1.0\nasync-javascript,2.0\nbrizy,2.4\n"
        issues = _check_plugin_conflicts(csv)
        assert len(issues) == 3

    def test_competing_page_builders(self):
        csv = "name,version\nelementor,3.18.2\nbeaver-builder-lite-version,2.8\n"
        issues = _check_plugin_conflicts(csv)
        assert any("beaver" in i.lower() for i in issues)

    def test_wp_rocket(self):
        csv = "name,version\nwp-rocket,3.15\n"
        issues = _check_plugin_conflicts(csv)
        assert any("WP Rocket" in i for i in issues)


# ── Elementor data integrity ──


class TestCheckElementorData:
    def test_valid_json(self):
        data = json.dumps([
            {
                "elType": "section",
                "elements": [
                    {
                        "elType": "column",
                        "elements": [
                            {
                                "elType": "widget",
                                "widgetType": "heading",
                                "settings": {"title": "Hello"},
                                "elements": [],
                            }
                        ],
                    }
                ],
            }
        ])
        issues = _check_elementor_data(data)
        assert issues == []

    def test_invalid_json(self):
        issues = _check_elementor_data("{broken json")
        assert any("CORRUPT" in i for i in issues)

    def test_unbalanced_braces(self):
        issues = _check_elementor_data("{{{")
        assert any("CORRUPT" in i for i in issues)
        assert any("Unbalanced" in i for i in issues)

    def test_not_array(self):
        issues = _check_elementor_data('{"key": "value"}')
        assert any("not an array" in i for i in issues)

    def test_broken_html_widget(self):
        data = json.dumps([
            {
                "elType": "widget",
                "widgetType": "html",
                "settings": {"html": "<script>var x = 1;"},
                "elements": [],
            }
        ])
        issues = _check_elementor_data(data)
        assert any("UNCLOSED <script>" in i for i in issues)

    def test_broken_text_editor(self):
        data = json.dumps([
            {
                "elType": "widget",
                "widgetType": "text-editor",
                "settings": {"editor": "<div><div><div><div>content</div>"},
                "elements": [],
            }
        ])
        issues = _check_elementor_data(data)
        assert any("unclosed <div>" in i for i in issues)

    def test_malformed_shortcode_in_widget(self):
        data = json.dumps([
            {
                "elType": "widget",
                "widgetType": "shortcode",
                "settings": {"shortcode": "[broken param='x'"},
                "elements": [],
            }
        ])
        issues = _check_elementor_data(data)
        assert any("MALFORMED SHORTCODE" in i for i in issues)

    def test_empty_data(self):
        assert _check_elementor_data("") == []

    def test_malformed_element(self):
        data = json.dumps(["not a dict", {"elType": "section", "elements": []}])
        issues = _check_elementor_data(data)
        assert any("MALFORMED ELEMENT" in i for i in issues)

    def test_null_bytes(self):
        issues = _check_elementor_data('{"key\\u0000": "val"}broken')
        assert any("CORRUPT" in i for i in issues)


# ── Rendered page analysis ──


class TestAnalyzeElementorPage:
    def test_no_elementor_markup(self):
        html = "<html><body><p>Hello</p></body></html>"
        issues = _analyze_elementor_page(html)
        assert any("No Elementor markup" in i for i in issues)

    def test_widget_error_markers(self):
        html = '<div class="elementor-widget-error">Widget failed</div>'
        issues = _analyze_elementor_page(html)
        assert any("WIDGET ERROR" in i for i in issues)

    def test_unrendered_template_shortcode(self):
        html = (
            '<div class="elementor-section">'
            '[elementor-template id="123"]'
            '</div>'
        )
        issues = _analyze_elementor_page(html)
        assert any("UNRENDERED" in i for i in issues)

    def test_duplicate_jquery(self):
        html = (
            '<div class="elementor-section">'
            '<script src="/wp-includes/js/jquery.min.js"></script>'
            '<script src="/theme/js/jquery.min.js"></script>'
            '</div>'
        )
        issues = _analyze_elementor_page(html)
        assert any("jQuery loaded" in i for i in issues)

    def test_missing_frontend_js(self):
        html = (
            '<div class="elementor-section">'
            '<div class="elementor-widget">content</div>'
            '</div>'
        )
        issues = _analyze_elementor_page(html)
        assert any("elementor-frontend.js NOT loaded" in i for i in issues)

    def test_large_inline_css(self):
        css_content = "a" * 250_000
        html = (
            f'<style id="elementor-frontend-inline-css">{css_content}</style>'
            '<div class="elementor-section">content</div>'
            '<script src="elementor-frontend.js"></script>'
        )
        issues = _analyze_elementor_page(html)
        assert any("inline Elementor CSS" in i for i in issues)

    def test_double_escaped_html(self):
        html = (
            '<div class="elementor-widget-container">'
            '&lt;script&gt;alert("xss")&lt;/script&gt;'
            '</div>'
            '<script src="elementor-frontend.js"></script>'
        )
        issues = _analyze_elementor_page(html)
        assert any("double-escaped" in i for i in issues)

    def test_clean_page(self):
        html = (
            '<html><head>'
            '<script src="/wp-includes/js/jquery.min.js"></script>'
            '</head><body>'
            '<div class="elementor-section">'
            '<div class="elementor-column">'
            '<div class="elementor-widget"><p>Hello</p></div>'
            '</div></div>'
            '<script src="elementor-frontend.js"></script>'
            '</body></html>'
        )
        issues = _analyze_elementor_page(html)
        assert issues == []


# ── Full report builder ──


class TestBuildElementorReport:
    def _make_data(self, **overrides):
        """Build a data dict with defaults."""
        defaults = {
            "elementor_version": ToolResult(output="3.18.2"),
            "elementor_pro_version": ToolResult(output="3.18.2"),
            "elementor_status": ToolResult(output="active"),
            "elementor_pro_status": ToolResult(output="active"),
            "css_print_method": ToolResult(output="external"),
            "disable_typography": ToolResult(output=""),
            "experiment_container": ToolResult(output=""),
            "dom_output": ToolResult(output=""),
            "optimized_markup": ToolResult(output=""),
            "css_dir": ToolResult(output="total 32\n-rw-r--r-- 1 user user 4096 post-1.css\n"),
            "elementor_posts": ToolResult(output="1\tHome\t5000\n2\tAbout\t3000"),
            "active_plugins": ToolResult(output="name,version\nelementor,3.18.2\n"),
            "page_html": ToolResult(
                output='<div class="elementor-section"><div class="elementor-widget">'
                       'content</div></div><script src="elementor-frontend.js"></script>'
            ),
            "php_errors": ToolResult(output="", error="No such file", exit_code=1),
            "active_theme": ToolResult(output="name,version\nhello-elementor,3.0"),
            "wp_version": ToolResult(output="6.4.2"),
            "page_elementor_data": ToolResult(output=""),
        }
        defaults.update(overrides)
        return defaults

    def test_healthy_report(self):
        data = self._make_data()
        report = _build_elementor_report("example.com", "https://example.com/", "/home/u/public_html", data)
        assert "Elementor Diagnostics" in report
        assert "3.18.2" in report
        assert "hello-elementor" in report

    def test_version_mismatch(self):
        data = self._make_data(
            elementor_version=ToolResult(output="3.18.2"),
            elementor_pro_version=ToolResult(output="3.17.0"),
        )
        report = _build_elementor_report("example.com", "https://example.com/", "/p", data)
        assert "VERSION MISMATCH" in report

    def test_internal_css_method(self):
        data = self._make_data(css_print_method=ToolResult(output="internal"))
        report = _build_elementor_report("example.com", "https://example.com/", "/p", data)
        assert "CSS PRINT METHOD" in report

    def test_missing_elementor(self):
        data = self._make_data(
            elementor_version=ToolResult(output="", error="not found", exit_code=1),
        )
        report = _build_elementor_report("example.com", "https://example.com/", "/p", data)
        assert "NOT FOUND" in report

    def test_php_errors_in_report(self):
        data = self._make_data(
            php_errors=ToolResult(output="[15-Mar-2026] Fatal error in elementor/includes/base.php"),
        )
        report = _build_elementor_report("example.com", "https://example.com/", "/p", data)
        assert "PHP Errors" in report
        assert "Fatal error" in report

    def test_corrupt_elementor_data(self):
        data = self._make_data(
            page_elementor_data=ToolResult(output="{broken json!!"),
        )
        report = _build_elementor_report("example.com", "https://example.com/", "/p", data)
        assert "CORRUPT" in report

    def test_recommended_actions_present(self):
        data = self._make_data(
            elementor_version=ToolResult(output="3.18.2"),
            elementor_pro_version=ToolResult(output="3.17.0"),
            css_print_method=ToolResult(output="internal"),
        )
        report = _build_elementor_report("example.com", "https://example.com/", "/p", data)
        assert "Recommended Actions" in report
        assert "Update Elementor" in report
        assert "CSS print method" in report
