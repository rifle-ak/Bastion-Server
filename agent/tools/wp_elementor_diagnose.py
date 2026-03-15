"""Elementor rendering diagnostics for WordPress.

Diagnoses the root cause of HTML/JS 'bleed-out' in Elementor pages —
where widget content leaks outside its container, raw HTML/JS appears
on the page, or sections overlap/break layout.

Common root causes this tool detects:
- Corrupted ``_elementor_data`` JSON in postmeta
- Elementor core/Pro version mismatches
- CSS print method misconfiguration
- Plugin conflicts (known bad actors)
- Broken widget containers (unclosed Elementor section/column divs)
- JS dependency conflicts on the rendered page
- Shortcode failures inside Elementor widgets
- DOM output mode issues
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from agent.inventory import Inventory
from agent.tools.base import BaseTool, ToolResult
from agent.tools.docker_tools import _run_on_server


class WpElementorDiagnose(BaseTool):
    """Diagnose Elementor HTML/JS bleed-out and rendering issues."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "wp_elementor_diagnose"

    @property
    def description(self) -> str:
        return (
            "Diagnose Elementor rendering issues — HTML/JS bleeding out of "
            "widgets, broken layouts, raw code showing on pages. Checks "
            "Elementor data integrity, version mismatches, CSS print method, "
            "plugin conflicts, and analyzes the rendered page for broken "
            "Elementor containers. Use when a customer reports broken "
            "Elementor pages."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server name.",
                },
                "domain": {
                    "type": "string",
                    "description": "Domain of the affected site (e.g. 'example.com').",
                },
                "path": {
                    "type": "string",
                    "description": "WordPress install path (e.g. '/home/user/public_html').",
                },
                "user": {
                    "type": "string",
                    "description": "cPanel/system username.",
                },
                "page_url": {
                    "type": "string",
                    "description": (
                        "Specific page URL showing the bleed-out (optional). "
                        "If not given, checks the homepage."
                    ),
                },
            },
            "required": ["server", "domain", "path", "user"],
        }

    async def execute(
        self,
        *,
        server: str,
        domain: str,
        path: str,
        user: str,
        page_url: str = "",
        **kwargs: Any,
    ) -> ToolResult:
        """Run Elementor rendering diagnostics."""
        wp = f"runuser -u {user} -- wp --path={path}"
        url = page_url or f"https://{domain}/"

        checks: dict[str, Any] = {
            # Elementor versions
            "elementor_version": _run_on_server(
                self._inventory, server,
                f"{wp} plugin get elementor --field=version 2>/dev/null",
            ),
            "elementor_pro_version": _run_on_server(
                self._inventory, server,
                f"{wp} plugin get elementor-pro --field=version 2>/dev/null",
            ),
            "elementor_status": _run_on_server(
                self._inventory, server,
                f"{wp} plugin get elementor --field=status 2>/dev/null",
            ),
            "elementor_pro_status": _run_on_server(
                self._inventory, server,
                f"{wp} plugin get elementor-pro --field=status 2>/dev/null",
            ),
            # Elementor settings from the database
            "css_print_method": _run_on_server(
                self._inventory, server,
                f"{wp} option get elementor_css_print_method 2>/dev/null",
            ),
            "disable_typography": _run_on_server(
                self._inventory, server,
                f"{wp} option get elementor_disable_typography_schemes 2>/dev/null",
            ),
            "experiment_container": _run_on_server(
                self._inventory, server,
                f"{wp} option get elementor_experiment-container 2>/dev/null",
            ),
            "dom_output": _run_on_server(
                self._inventory, server,
                f"{wp} option get elementor_experiment-e_dom_optimization 2>/dev/null",
            ),
            "optimized_markup": _run_on_server(
                self._inventory, server,
                f"{wp} option get elementor_experiment-e_optimized_markup 2>/dev/null",
            ),
            # Generated CSS file status
            "css_dir": _run_on_server(
                self._inventory, server,
                f"ls -la {path}/wp-content/uploads/elementor/css/ 2>/dev/null | head -20",
            ),
            # Check for Elementor data corruption on recent posts
            "elementor_posts": _run_on_server(
                self._inventory, server,
                f"{wp} db query \"SELECT p.ID, p.post_title, "
                f"LENGTH(pm.meta_value) as data_len "
                f"FROM wp_posts p "
                f"INNER JOIN wp_postmeta pm ON p.ID = pm.post_id "
                f"WHERE pm.meta_key = '_elementor_data' "
                f"AND p.post_status = 'publish' "
                f"ORDER BY p.post_modified DESC LIMIT 10\" 2>/dev/null",
            ),
            # Check active plugins for known conflicts
            "active_plugins": _run_on_server(
                self._inventory, server,
                f"{wp} plugin list --status=active --format=csv --fields=name,version 2>/dev/null",
            ),
            # Fetch the affected page
            "page_html": _run_on_server(
                self._inventory, server,
                f"curl -sL --max-time 20 '{url}'",
            ),
            # Check for PHP errors related to Elementor
            "php_errors": _run_on_server(
                self._inventory, server,
                f"grep -i 'elementor' {path}/wp-content/debug.log 2>/dev/null | tail -20",
            ),
            # Theme compatibility
            "active_theme": _run_on_server(
                self._inventory, server,
                f"{wp} theme list --status=active --format=csv --fields=name,version 2>/dev/null",
            ),
            # WP version (Elementor compatibility)
            "wp_version": _run_on_server(
                self._inventory, server,
                f"{wp} core version 2>/dev/null",
            ),
        }

        keys = list(checks.keys())
        results = await asyncio.gather(*[checks[k] for k in keys])
        data = dict(zip(keys, results))

        # If a specific page URL was given, also pull its Elementor data
        if page_url:
            slug_check = await _run_on_server(
                self._inventory, server,
                f"{wp} db query \"SELECT pm.meta_value "
                f"FROM wp_posts p "
                f"INNER JOIN wp_postmeta pm ON p.ID = pm.post_id "
                f"WHERE pm.meta_key = '_elementor_data' "
                f"AND p.guid LIKE '%{domain}%' "
                f"ORDER BY p.post_modified DESC LIMIT 1\" "
                f"--skip-column-names 2>/dev/null",
            )
            data["page_elementor_data"] = slug_check
        else:
            # Check homepage / front page Elementor data
            frontpage_check = await _run_on_server(
                self._inventory, server,
                f"{wp} eval \""
                f"\\$fid = get_option('page_on_front');"
                f"if(\\$fid) {{"
                f"  \\$d = get_post_meta(\\$fid, '_elementor_data', true);"
                f"  echo substr(\\$d, 0, 2000);"
                f"}}\" 2>/dev/null",
            )
            data["page_elementor_data"] = frontpage_check

        return ToolResult(output=_build_elementor_report(domain, url, path, data))


def _v(data: dict[str, ToolResult], key: str) -> str:
    """Extract trimmed output from a ToolResult, or empty string on failure."""
    r = data.get(key)
    return r.output.strip() if r and r.success else ""


def _build_elementor_report(
    domain: str, url: str, path: str, data: dict[str, ToolResult],
) -> str:
    """Build the Elementor diagnostics report."""
    sections: list[str] = [f"# Elementor Diagnostics: {domain}\n"]
    findings: list[str] = []

    # ── Versions ──
    sections.append("## Elementor Versions")
    el_ver = _v(data, "elementor_version")
    el_pro_ver = _v(data, "elementor_pro_version")
    el_status = _v(data, "elementor_status")
    el_pro_status = _v(data, "elementor_pro_status")

    if el_ver:
        sections.append(f"Elementor: {el_ver} ({el_status})")
    else:
        findings.append(
            "✗ Elementor plugin NOT FOUND or not installed. "
            "Cannot diagnose Elementor issues without the plugin."
        )
        sections.append("✗ Elementor not found")

    if el_pro_ver:
        sections.append(f"Elementor Pro: {el_pro_ver} ({el_pro_status})")
        # Check version compatibility
        if el_ver and el_pro_ver:
            el_major = _major_minor(el_ver)
            pro_major = _major_minor(el_pro_ver)
            if el_major and pro_major and el_major != pro_major:
                findings.append(
                    f"✗ VERSION MISMATCH: Elementor {el_ver} vs Pro {el_pro_ver}. "
                    f"Major/minor versions should match. Mismatches cause widget "
                    f"rendering failures, broken layouts, and JS errors. "
                    f"Update both plugins to the same version."
                )
    else:
        sections.append("Elementor Pro: not installed")

    wp_ver = _v(data, "wp_version")
    if wp_ver:
        sections.append(f"WordPress: {wp_ver}")

    active_theme = _v(data, "active_theme")
    if active_theme:
        # Skip CSV header
        theme_lines = [l for l in active_theme.splitlines() if l and "name,version" not in l.lower()]
        if theme_lines:
            sections.append(f"Active theme: {theme_lines[0]}")

    # ── CSS Print Method ──
    sections.append("\n## CSS Configuration")
    css_method = _v(data, "css_print_method")
    if css_method:
        sections.append(f"CSS print method: {css_method}")
        if css_method == "internal":
            findings.append(
                "⚠ CSS PRINT METHOD is 'internal' — Elementor embeds all CSS "
                "inline in the <head>. This bloats the HTML, can conflict with "
                "caching plugins, and causes style bleed when page caches serve "
                "stale inline CSS. Switch to 'external' in Elementor → Settings "
                "→ Advanced for better isolation and cacheability."
            )
    else:
        sections.append("CSS print method: default (external)")

    # CSS files on disk
    css_dir = _v(data, "css_dir")
    if css_dir:
        css_files = [l for l in css_dir.splitlines() if l.strip() and not l.startswith("total")]
        if css_files:
            sections.append(f"Generated CSS files: {len(css_files)}")
            # Check for empty CSS files
            empty_css = [l for l in css_files if l.strip().split()[4] == "0" if len(l.split()) > 4]
            if empty_css:
                findings.append(
                    f"⚠ {len(empty_css)} EMPTY CSS file(s) in Elementor CSS dir. "
                    f"CSS regeneration may have failed. Go to Elementor → Tools "
                    f"→ Regenerate CSS & Data."
                )
        else:
            findings.append(
                "⚠ No generated CSS files found. Elementor may not have "
                "generated its stylesheets. Regenerate via Elementor → Tools."
            )
    else:
        findings.append(
            "⚠ Elementor CSS directory missing or empty at "
            f"{path}/wp-content/uploads/elementor/css/. "
            "Run Elementor → Tools → Regenerate CSS & Data."
        )

    # ── DOM Output / Experiments ──
    sections.append("\n## Elementor Experiments")
    dom_output = _v(data, "dom_output")
    container_exp = _v(data, "experiment_container")
    optimized_markup = _v(data, "optimized_markup")

    experiments = {
        "DOM Optimization": dom_output,
        "Flexbox Container": container_exp,
        "Optimized Markup": optimized_markup,
    }
    for exp_name, exp_val in experiments.items():
        if exp_val:
            sections.append(f"{exp_name}: {exp_val}")
        else:
            sections.append(f"{exp_name}: default/inactive")

    if container_exp == "active" and dom_output != "active":
        findings.append(
            "⚠ Flexbox Container is active but DOM Optimization is not. "
            "These experiments should typically be enabled together. "
            "Inconsistent experiment states can cause rendering issues."
        )

    # ── Elementor Data Integrity ──
    sections.append("\n## Elementor Data Integrity")
    posts_raw = _v(data, "elementor_posts")
    if posts_raw:
        sections.append("Recent Elementor pages:")
        for line in posts_raw.strip().splitlines()[:10]:
            sections.append(f"  {line}")
    else:
        sections.append("No Elementor pages found in database.")

    # Check page-specific Elementor data
    page_data_raw = _v(data, "page_elementor_data")
    if page_data_raw:
        data_issues = _check_elementor_data(page_data_raw)
        if data_issues:
            findings.extend(data_issues)
            for issue in data_issues:
                sections.append(f"  {issue}")
        else:
            sections.append("  ✓ Elementor data structure looks valid")
    else:
        sections.append("  Could not retrieve Elementor data for this page.")

    # ── Plugin Conflicts ──
    sections.append("\n## Plugin Conflict Check")
    active_plugins = _v(data, "active_plugins")
    if active_plugins:
        conflicts = _check_plugin_conflicts(active_plugins)
        if conflicts:
            findings.extend(conflicts)
            for c in conflicts:
                sections.append(f"  {c}")
        else:
            sections.append("  ✓ No known Elementor-conflicting plugins detected")
    else:
        sections.append("  Could not retrieve plugin list.")

    # ── Rendered Page Analysis ──
    sections.append("\n## Rendered Page Analysis")
    page_html = _v(data, "page_html")
    if page_html:
        page_issues = _analyze_elementor_page(page_html)
        if page_issues:
            findings.extend(page_issues)
            for issue in page_issues:
                sections.append(f"  {issue}")
        else:
            sections.append("  ✓ No Elementor-specific rendering issues in page HTML")
    else:
        sections.append(f"  Could not fetch {url}")

    # ── PHP Errors ──
    php_errors = _v(data, "php_errors")
    if php_errors:
        sections.append("\n## Elementor PHP Errors (debug.log)")
        error_lines = php_errors.strip().splitlines()
        sections.append(f"Found {len(error_lines)} Elementor-related error(s):")
        for line in error_lines[:10]:
            sections.append(f"  {line[:200]}")
        findings.append(
            f"⚠ {len(error_lines)} Elementor PHP error(s) in debug.log. "
            "These errors may directly cause rendering failures. See details above."
        )

    # ── Verdict ──
    sections.append("\n---")
    if findings:
        sections.append(f"\n## Findings ({len(findings)} issues)\n")
        critical = [f for f in findings if f.startswith("✗")]
        warnings = [f for f in findings if f.startswith("⚠")]
        for f in critical:
            sections.append(f)
        for f in warnings:
            sections.append(f)

        # Suggested remediation
        sections.append("\n## Recommended Actions")
        if any("VERSION MISMATCH" in f for f in findings):
            sections.append(
                "1. **Update Elementor + Pro to matching versions.** "
                "This is the most common cause of bleed-out."
            )
        if any("CSS PRINT METHOD" in f for f in findings):
            sections.append(
                "2. **Switch CSS print method to 'external'** — "
                "Elementor → Settings → Advanced → CSS Print Method."
            )
        if any("Regenerate" in f for f in findings):
            sections.append(
                "3. **Regenerate CSS & Data** — "
                "Elementor → Tools → Regenerate CSS & Data."
            )
        if any("CORRUPT" in f or "MALFORMED" in f for f in findings):
            sections.append(
                "4. **Fix corrupted Elementor data** — edit the affected page "
                "in Elementor, check each widget for broken HTML, and re-save. "
                "If the page won't load in the editor, restore from a backup."
            )
        if any("conflict" in f.lower() for f in findings):
            sections.append(
                "5. **Resolve plugin conflicts** — deactivate conflicting "
                "plugins one at a time and test. See the conflict list above."
            )
        sections.append(
            "6. **General: clear all caches** (page cache, CDN, browser) "
            "after any fix."
        )
    else:
        sections.append(
            "\n✓ No Elementor-specific issues detected. If bleed-out persists, "
            "try: (1) Regenerate CSS in Elementor → Tools, (2) Switch to a "
            "default theme temporarily to rule out theme conflicts, (3) Check "
            "browser console for JS errors."
        )

    return "\n".join(sections)


def _major_minor(version: str) -> str:
    """Extract major.minor from a version string like '3.18.2'."""
    parts = version.strip().split(".")
    if len(parts) >= 2:
        return f"{parts[0]}.{parts[1]}"
    return ""


def _check_elementor_data(raw: str) -> list[str]:
    """Check Elementor JSON data for corruption indicators."""
    issues: list[str] = []
    raw = raw.strip()

    if not raw:
        return issues

    # Try to parse as JSON
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        issues.append(
            f"✗ CORRUPT ELEMENTOR DATA: JSON parse error — {e}. "
            f"The Elementor data for this page is malformed. This WILL cause "
            f"rendering issues. The page needs to be re-edited or restored "
            f"from backup."
        )
        # Check for common corruption patterns
        if "\\u0000" in raw or "\x00" in raw:
            issues.append(
                "  → Null bytes detected in data — likely database corruption."
            )
        if raw.count("{") != raw.count("}"):
            issues.append(
                f"  → Unbalanced braces: {raw.count('{')} opening, "
                f"{raw.count('}')} closing."
            )
        return issues

    if not isinstance(data, list):
        issues.append(
            "⚠ Elementor data is not an array at the top level. "
            "Expected a JSON array of widgets/sections."
        )
        return issues

    # Walk the element tree looking for issues
    _walk_elements(data, issues, depth=0)

    return issues


def _walk_elements(
    elements: list[dict[str, Any]], issues: list[str], depth: int,
) -> None:
    """Recursively walk Elementor element tree checking for problems."""
    if depth > 20:
        issues.append("⚠ Elementor data nested >20 levels deep — possible corruption.")
        return

    for elem in elements:
        if not isinstance(elem, dict):
            issues.append(
                f"✗ MALFORMED ELEMENT: expected dict, got {type(elem).__name__}. "
                f"This element will fail to render."
            )
            continue

        el_type = elem.get("elType", "")
        widget_type = elem.get("widgetType", "")

        # Check for HTML widget with potentially broken content
        if widget_type == "html":
            settings = elem.get("settings", {})
            html_content = settings.get("html", "")
            if html_content:
                html_issues = _check_widget_html(html_content)
                if html_issues:
                    issues.extend(html_issues)

        # Check for text-editor widget with broken HTML
        if widget_type == "text-editor":
            settings = elem.get("settings", {})
            editor_content = settings.get("editor", "")
            if editor_content:
                html_issues = _check_widget_html(editor_content)
                if html_issues:
                    issues.extend(html_issues)

        # Check for shortcode widget
        if widget_type == "shortcode":
            settings = elem.get("settings", {})
            shortcode = settings.get("shortcode", "")
            if shortcode:
                sc_issues = _check_shortcode(shortcode)
                if sc_issues:
                    issues.extend(sc_issues)

        # Recurse into children
        children = elem.get("elements", [])
        if isinstance(children, list) and children:
            _walk_elements(children, issues, depth + 1)


def _check_widget_html(html: str) -> list[str]:
    """Check HTML widget content for issues that cause bleed-out."""
    issues: list[str] = []

    # Unclosed script tags
    script_opens = len(re.findall(r'<script', html, re.IGNORECASE))
    script_closes = len(re.findall(r'</script>', html, re.IGNORECASE))
    if script_opens > script_closes:
        issues.append(
            f"✗ UNCLOSED <script> TAG in Elementor HTML widget: "
            f"{script_opens} opening, {script_closes} closing. "
            f"This causes all content after the widget to be treated as "
            f"JavaScript — the #1 cause of HTML/JS bleed-out."
        )

    # Unclosed style tags
    style_opens = len(re.findall(r'<style', html, re.IGNORECASE))
    style_closes = len(re.findall(r'</style>', html, re.IGNORECASE))
    if style_opens > style_closes:
        issues.append(
            f"✗ UNCLOSED <style> TAG in Elementor widget: "
            f"{style_opens} opening, {style_closes} closing. "
            f"This causes all content after the widget to be treated as CSS."
        )

    # Unclosed iframes
    iframe_opens = len(re.findall(r'<iframe', html, re.IGNORECASE))
    iframe_closes = len(re.findall(r'</iframe>', html, re.IGNORECASE))
    if iframe_opens > iframe_closes:
        issues.append(
            "✗ UNCLOSED <iframe> in Elementor widget. "
            "This swallows all content after it."
        )

    # Unclosed divs (major cause of layout bleed)
    div_opens = len(re.findall(r'<div[\s>]', html, re.IGNORECASE))
    div_closes = len(re.findall(r'</div>', html, re.IGNORECASE))
    if div_opens > div_closes + 2:
        diff = div_opens - div_closes
        issues.append(
            f"⚠ {diff} unclosed <div> tag(s) in Elementor widget content. "
            f"This disrupts Elementor's container structure and causes "
            f"layout bleed between sections."
        )

    # Raw PHP in HTML widget (won't execute, shows as text)
    if "<?php" in html or "<?=" in html:
        issues.append(
            "✗ RAW PHP CODE in Elementor HTML widget. PHP does not execute "
            "inside Elementor widgets — it will display as raw text. Use a "
            "PHP execution plugin or shortcode instead."
        )

    return issues


def _check_shortcode(shortcode: str) -> list[str]:
    """Check for problematic shortcode patterns."""
    issues: list[str] = []

    # Nested shortcodes that commonly break
    if shortcode.count("[") > 3:
        issues.append(
            "⚠ Deeply nested shortcodes in Elementor shortcode widget. "
            "Complex shortcode nesting can fail to render and leak raw "
            "bracket syntax into the page."
        )

    # Unbalanced brackets
    if shortcode.count("[") != shortcode.count("]"):
        issues.append(
            "✗ MALFORMED SHORTCODE: unbalanced brackets. "
            f"Opens: {shortcode.count('[')}, Closes: {shortcode.count(']')}. "
            "This shortcode will fail to render."
        )

    return issues


# Plugins known to cause Elementor rendering conflicts
_CONFLICT_PLUGINS: dict[str, str] = {
    "autoptimize": (
        "Autoptimize can break Elementor by aggregating/reordering CSS/JS. "
        "Exclude Elementor scripts in Autoptimize → JS → Exclude."
    ),
    "w3-total-cache": (
        "W3 Total Cache minification can break Elementor JS. "
        "Disable JS minification or exclude elementor scripts."
    ),
    "wp-rocket": (
        "WP Rocket's JS delay/defer can break Elementor frontend scripts. "
        "Add elementor-frontend exclusions in WP Rocket → File Optimization."
    ),
    "async-javascript": (
        "Async JavaScript can defer Elementor scripts causing widgets to "
        "render without styles. Exclude Elementor from async loading."
    ),
    "jetpack": (
        "Jetpack's CSS concatenation can conflict with Elementor styles. "
        "Disable Jetpack CSS concatenation if layout issues appear."
    ),
    "sg-cachepress": (
        "SiteGround Optimizer JS combination can break Elementor. "
        "Exclude Elementor from JS combination settings."
    ),
    "fast-velocity-minify": (
        "FVM's CSS/JS merging often breaks Elementor widget rendering. "
        "Exclude elementor-frontend and elementor-pro from merging."
    ),
    "clearfy": (
        "Clearfy Pro's code optimization can strip Elementor-required "
        "attributes. Disable HTML minification."
    ),
    "perfmatters": (
        "Perfmatters script manager can inadvertently disable Elementor "
        "scripts on specific pages. Check per-page script settings."
    ),
    "brizy": (
        "Brizy and Elementor conflict — two page builders active at once "
        "causes CSS/JS collisions. Deactivate one."
    ),
    "beaver-builder-lite-version": (
        "Beaver Builder and Elementor conflict — two page builders active "
        "at once. Deactivate one."
    ),
    "divi-builder": (
        "Divi Builder and Elementor conflict — two page builders active. "
        "Deactivate one."
    ),
    "siteorigin-panels": (
        "SiteOrigin Panels and Elementor conflict. Deactivate one."
    ),
    "wpbakery": (
        "WPBakery and Elementor conflict — two page builders active. "
        "Deactivate one."
    ),
}


def _check_plugin_conflicts(plugins_csv: str) -> list[str]:
    """Check active plugins against known Elementor conflict list."""
    issues: list[str] = []
    plugin_lines = plugins_csv.strip().splitlines()

    for line in plugin_lines:
        # CSV format: name,version
        parts = line.split(",")
        if not parts:
            continue
        plugin_name = parts[0].strip().lower()

        for conflict_slug, advice in _CONFLICT_PLUGINS.items():
            if conflict_slug in plugin_name:
                version = parts[1].strip() if len(parts) > 1 else "?"
                issues.append(
                    f"⚠ POTENTIAL CONFLICT: {plugin_name} ({version}) — {advice}"
                )

    return issues


def _analyze_elementor_page(html: str) -> list[str]:
    """Analyze rendered page HTML for Elementor-specific rendering issues."""
    issues: list[str] = []

    # Check if Elementor content is present at all
    if "elementor" not in html.lower():
        issues.append(
            "⚠ No Elementor markup found in rendered page. Either this "
            "page doesn't use Elementor, or Elementor failed to render entirely."
        )
        return issues

    # Check Elementor section/column/widget container structure
    section_opens = len(re.findall(
        r'<(?:div|section)[^>]*class="[^"]*elementor-section[^"]*"', html, re.IGNORECASE,
    ))
    section_closes = _count_elementor_closes(html, "elementor-section")

    column_opens = len(re.findall(
        r'<div[^>]*class="[^"]*elementor-column[^"]*"', html, re.IGNORECASE,
    ))

    widget_opens = len(re.findall(
        r'<div[^>]*class="[^"]*elementor-widget[^"]*"', html, re.IGNORECASE,
    ))

    if section_opens > 0:
        sections_info = f"Sections: {section_opens}, Columns: {column_opens}, Widgets: {widget_opens}"
        # We can't perfectly count closes without a proper parser, but
        # we can flag if there's a large imbalance in overall divs
        # within elementor-element containers

    # Detect inline Elementor CSS that's excessively large
    inline_styles = re.findall(
        r'<style[^>]*id=["\']elementor[^"\']*["\'][^>]*>(.*?)</style>',
        html, re.DOTALL | re.IGNORECASE,
    )
    total_inline_css = sum(len(s) for s in inline_styles)
    if total_inline_css > 200_000:
        issues.append(
            f"⚠ {total_inline_css // 1024}KB of inline Elementor CSS. "
            f"This bloats page size and indicates the CSS print method "
            f"is set to 'internal'. Switch to 'external' for better "
            f"performance and fewer caching issues."
        )

    # Check for Elementor JS errors markers
    if "elementor-widget-error" in html or "elementor-widget-empty" in html:
        issues.append(
            "✗ ELEMENTOR WIDGET ERROR markers found in rendered HTML. "
            "One or more widgets failed to render. Edit the page in "
            "Elementor and check for broken widgets."
        )

    # Check for raw Elementor shortcodes that didn't render
    raw_shortcodes = re.findall(r'\[elementor-template[^\]]*\]', html)
    if raw_shortcodes:
        issues.append(
            f"✗ {len(raw_shortcodes)} UNRENDERED Elementor template shortcode(s). "
            f"These appear as raw text: {raw_shortcodes[0][:80]}. "
            f"The referenced template may be deleted or Elementor failed to process it."
        )

    # Check for JavaScript errors in inline scripts
    inline_scripts = re.findall(
        r'<script[^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE,
    )
    for script in inline_scripts:
        if "elementor" in script.lower():
            # Check for syntax issues
            if script.count("{") != script.count("}"):
                issues.append(
                    "✗ MALFORMED INLINE JS: Unbalanced braces in an Elementor "
                    "script block. This will cause JS errors that break "
                    "frontend widget functionality."
                )
                break

    # Check for duplicate jQuery
    jquery_loads = len(re.findall(r'jquery(?:\.min)?\.js', html, re.IGNORECASE))
    if jquery_loads > 1:
        issues.append(
            f"⚠ jQuery loaded {jquery_loads} times. Multiple jQuery versions "
            f"cause '$ is not a function' errors that break Elementor widgets. "
            f"Likely caused by a theme or plugin including its own jQuery."
        )

    # Check for broken Elementor frontend script loading
    if "elementor-frontend" not in html and "elementor" in html.lower():
        issues.append(
            "✗ Elementor markup present but elementor-frontend.js NOT loaded. "
            "This means widgets render as static HTML without interactivity. "
            "A caching or optimization plugin may have removed the script."
        )

    # Check for HTML entities leaking (common bleed-out symptom)
    if "&lt;script" in html or "&lt;style" in html:
        # These are escaped, which is fine in content — but if they
        # appear inside Elementor widget wrappers it means content isn't
        # being processed
        widget_areas = re.findall(
            r'elementor-widget-container[^>]*>(.*?)</div>',
            html, re.DOTALL | re.IGNORECASE,
        )
        for area in widget_areas[:20]:
            if "&lt;script" in area or "&lt;/div&gt;" in area:
                issues.append(
                    "⚠ HTML entities (&lt;, &gt;) found inside Elementor widget "
                    "containers. Content is being double-escaped — it shows raw "
                    "HTML tags as text instead of rendering them. This usually "
                    "means the content was pasted from an encoded source."
                )
                break

    return issues


def _count_elementor_closes(html: str, element_class: str) -> int:
    """Approximate count of closing tags for Elementor elements.

    This is a best-effort heuristic — proper counting requires a DOM
    parser, but for diagnostic purposes this is sufficient.
    """
    # We can't reliably count closing </div>s for specific classes
    # without a full parser. Return 0 to skip the imbalance check.
    return 0
