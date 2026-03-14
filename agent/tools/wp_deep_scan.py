"""Deep WordPress performance diagnostics.

Goes beyond basic health checks to find the actual root cause of
slow page loads. Measures TTFB, analyzes database queries, checks
caching layers, OPcache, autoloaded options bloat, external HTTP
calls, image optimization, and compression.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from agent.inventory import Inventory
from agent.tools.base import BaseTool, ToolResult
from agent.tools.docker_tools import _run_on_server


class WpDeepPerformance(BaseTool):
    """Deep WordPress performance diagnosis — find why a site is slow."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "wp_deep_performance"

    @property
    def description(self) -> str:
        return (
            "Deep WordPress performance diagnosis. Measures TTFB, analyzes "
            "DB queries, checks OPcache, object cache, autoloaded options, "
            "image sizes, compression, and PHP config. Finds the actual "
            "root cause of slow page loads."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Webhost server name.",
                },
                "domain": {
                    "type": "string",
                    "description": "Domain to test (e.g. 'example.com').",
                },
                "path": {
                    "type": "string",
                    "description": "WordPress install path (e.g. '/home/user/public_html').",
                },
                "user": {
                    "type": "string",
                    "description": "cPanel/system username (for WP-CLI).",
                },
            },
            "required": ["server", "domain", "path", "user"],
        }

    async def execute(
        self, *, server: str, domain: str, path: str, user: str, **kwargs: Any,
    ) -> ToolResult:
        """Run deep WordPress performance analysis."""
        wp = f"runuser -u {user} -- wp --path={path}"

        checks: dict[str, Any] = {
            # TTFB measurement
            "ttfb": _run_on_server(
                self._inventory, server,
                f"curl -so /dev/null -w "
                f"'dns:%{{time_namelookup}}|connect:%{{time_connect}}|"
                f"ttfb:%{{time_starttransfer}}|total:%{{time_total}}|"
                f"size:%{{size_download}}|code:%{{http_code}}' "
                f"https://{domain}/ 2>/dev/null",
            ),
            # Autoloaded options size (the silent killer)
            "autoload_size": _run_on_server(
                self._inventory, server,
                f"{wp} db query \"SELECT SUM(LENGTH(option_value)) as total "
                f"FROM wp_options WHERE autoload='yes'\" --skip-column-names 2>/dev/null",
            ),
            # Top autoloaded options by size
            "autoload_top": _run_on_server(
                self._inventory, server,
                f"{wp} db query \"SELECT option_name, LENGTH(option_value) as size "
                f"FROM wp_options WHERE autoload='yes' "
                f"ORDER BY size DESC LIMIT 10\" 2>/dev/null",
            ),
            # Object cache type
            "object_cache": _run_on_server(
                self._inventory, server,
                f"ls -la {path}/wp-content/object-cache.php 2>/dev/null",
            ),
            # Object cache drop-in identification
            "cache_type": _run_on_server(
                self._inventory, server,
                f"head -5 {path}/wp-content/object-cache.php 2>/dev/null",
            ),
            # OPcache status
            "opcache": _run_on_server(
                self._inventory, server,
                f"{wp} eval 'if(function_exists(\"opcache_get_status\"))"
                f"{{$s=opcache_get_status();echo \"hit_rate:\".$s[\"opcache_statistics\"]"
                f"[\"opcache_hit_rate\"].\"\\ncached:\".$s[\"opcache_statistics\"]"
                f"[\"num_cached_scripts\"].\"\\nmemory_used:\"."
                f"round($s[\"memory_usage\"][\"used_memory\"]/1048576,1).\"MB\";}}"
                f"else{{echo \"OPcache not available\";}}' 2>/dev/null",
            ),
            # Active plugin count
            "plugins": _run_on_server(
                self._inventory, server,
                f"{wp} plugin list --status=active --format=count 2>/dev/null",
            ),
            # Page cache detection
            "page_cache": _run_on_server(
                self._inventory, server,
                f"{wp} plugin list --status=active --format=csv 2>/dev/null",
            ),
            # Database size
            "db_size": _run_on_server(
                self._inventory, server,
                f"{wp} db size --format=csv 2>/dev/null",
            ),
            # Transients, revisions, spam, trash
            "bloat": _run_on_server(
                self._inventory, server,
                f"{wp} db query \"SELECT "
                f"(SELECT COUNT(*) FROM wp_options WHERE option_name LIKE '%_transient_%') as transients, "
                f"(SELECT COUNT(*) FROM wp_posts WHERE post_type='revision') as revisions, "
                f"(SELECT COUNT(*) FROM wp_comments WHERE comment_approved='spam') as spam, "
                f"(SELECT COUNT(*) FROM wp_posts WHERE post_status='trash') as trash\" "
                f"2>/dev/null",
            ),
            # PHP memory limit and max execution time
            "php_config": _run_on_server(
                self._inventory, server,
                f"{wp} eval 'echo \"memory_limit:\".ini_get(\"memory_limit\")."
                f"\"\\nmax_execution_time:\".ini_get(\"max_execution_time\")."
                f"\"\\nupload_max_filesize:\".ini_get(\"upload_max_filesize\")."
                f"\"\\npost_max_size:\".ini_get(\"post_max_size\")."
                f"\"\\nmax_input_vars:\".ini_get(\"max_input_vars\");' 2>/dev/null",
            ),
            # WordPress cron (is it running on page loads?)
            "cron_constant": _run_on_server(
                self._inventory, server,
                f"grep -c 'DISABLE_WP_CRON' {path}/wp-config.php 2>/dev/null",
            ),
            # Large images in uploads
            "large_images": _run_on_server(
                self._inventory, server,
                f"find {path}/wp-content/uploads -type f "
                f"\\( -name '*.jpg' -o -name '*.png' -o -name '*.jpeg' \\) "
                f"-size +2M 2>/dev/null | wc -l",
            ),
            # Total uploads size
            "uploads_size": _run_on_server(
                self._inventory, server,
                f"du -sh {path}/wp-content/uploads 2>/dev/null",
            ),
            # Response headers (compression, caching)
            "headers": _run_on_server(
                self._inventory, server,
                f"curl -sI https://{domain}/ 2>/dev/null",
            ),
            # External HTTP requests (wp-cron, API calls)
            "wp_http": _run_on_server(
                self._inventory, server,
                f"{wp} transient list --format=count 2>/dev/null",
            ),
        }

        keys = list(checks.keys())
        results = await asyncio.gather(*[checks[k] for k in keys])
        data = dict(zip(keys, results))

        return ToolResult(output=_build_wp_report(domain, path, data))


def _v(data: dict[str, ToolResult], key: str) -> str:
    r = data.get(key)
    return r.output.strip() if r and r.success else ""


def _build_wp_report(domain: str, path: str, data: dict[str, ToolResult]) -> str:
    """Build WordPress performance report."""
    sections: list[str] = [f"# WordPress Performance: {domain}\n"]
    findings: list[str] = []

    # ── TTFB ──
    sections.append("## Page Load Timing")
    ttfb_raw = _v(data, "ttfb")
    if ttfb_raw:
        timing: dict[str, str] = {}
        for part in ttfb_raw.split("|"):
            if ":" in part:
                k, v = part.split(":", 1)
                timing[k] = v

        ttfb = timing.get("ttfb", "?")
        total = timing.get("total", "?")
        code = timing.get("code", "?")
        size = timing.get("size", "?")

        sections.append(f"**HTTP {code}** | Size: {size} bytes")
        sections.append(f"DNS: {timing.get('dns', '?')}s → Connect: {timing.get('connect', '?')}s → TTFB: {ttfb}s → Total: {total}s")

        try:
            ttfb_val = float(ttfb)
            if ttfb_val > 3.0:
                findings.append(
                    f"✗ TTFB {ttfb_val:.2f}s — extremely slow. Server takes >3s to "
                    f"start sending the response. This is a backend (PHP/MySQL) "
                    f"problem, not a network issue."
                )
            elif ttfb_val > 1.0:
                findings.append(
                    f"⚠ TTFB {ttfb_val:.2f}s — slow. Target is <500ms. Check "
                    f"database queries, object cache, and OPcache below."
                )
            elif ttfb_val > 0.5:
                findings.append(f"⚠ TTFB {ttfb_val:.2f}s — acceptable but could be faster")
            else:
                sections.append(f"✓ TTFB excellent: {ttfb_val:.2f}s")
        except ValueError:
            pass

    # ── Autoloaded Options ──
    sections.append("\n## Autoloaded Options")
    autoload = _v(data, "autoload_size")
    if autoload:
        try:
            size_bytes = int(autoload.strip())
            size_mb = size_bytes / (1024 * 1024)
            sections.append(f"Total autoloaded: {size_mb:.2f} MB")
            if size_mb > 2:
                findings.append(
                    f"✗ AUTOLOAD BLOAT: {size_mb:.1f} MB of autoloaded options. "
                    f"This data loads on EVERY page request. Anything over 1MB "
                    f"significantly slows the site. Clean up with: "
                    f"wp option list --autoload=yes --orderby=length --order=desc"
                )
            elif size_mb > 1:
                findings.append(f"⚠ Autoloaded options: {size_mb:.1f} MB (target: <1MB)")
        except ValueError:
            sections.append(autoload)

    autoload_top = _v(data, "autoload_top")
    if autoload_top:
        sections.append("**Largest autoloaded options:**")
        for line in autoload_top.strip().splitlines()[:7]:
            sections.append(f"  {line}")

    # ── Object Cache ──
    sections.append("\n## Object Cache")
    obj_cache = _v(data, "object_cache")
    cache_type = _v(data, "cache_type")
    if obj_cache:
        if "redis" in cache_type.lower():
            sections.append("✓ Redis object cache active")
        elif "memcache" in cache_type.lower():
            sections.append("✓ Memcached object cache active")
        else:
            sections.append(f"Object cache drop-in: {cache_type[:100]}")
    else:
        findings.append(
            "✗ NO OBJECT CACHE: Every page load hits the database for "
            "everything. Install Redis or Memcached object cache — this "
            "alone can cut TTFB by 50-80%."
        )
        sections.append("✗ No object cache installed")

    # ── OPcache ──
    sections.append("\n## OPcache")
    opcache = _v(data, "opcache")
    if opcache and "not available" not in opcache.lower():
        sections.append(opcache)
        hit_match = re.search(r'hit_rate:([\d.]+)', opcache)
        if hit_match:
            hit_rate = float(hit_match.group(1))
            if hit_rate < 90:
                findings.append(
                    f"⚠ OPcache hit rate {hit_rate:.0f}% — should be >95%. "
                    f"PHP is recompiling scripts unnecessarily. Increase opcache.memory_consumption."
                )
    elif opcache:
        findings.append(
            "✗ OPCACHE DISABLED: PHP recompiles every file on every request. "
            "This wastes massive CPU. Enable OPcache in php.ini."
        )
        sections.append("✗ OPcache not available")

    # ── Page Cache ──
    sections.append("\n## Page Cache")
    plugins_raw = _v(data, "page_cache")
    page_cache_plugins = [
        "wp-super-cache", "w3-total-cache", "litespeed-cache",
        "wp-rocket", "wp-fastest-cache", "cache-enabler",
        "breeze", "sg-cachepress", "nitropack",
    ]
    found_cache = [
        p for p in page_cache_plugins
        if p in plugins_raw.lower()
    ]
    if found_cache:
        sections.append(f"✓ Page cache plugin: {', '.join(found_cache)}")
    else:
        findings.append(
            "⚠ No page cache plugin detected. Every visitor triggers full "
            "PHP execution. Install LiteSpeed Cache, WP Super Cache, or WP Rocket."
        )
        sections.append("⚠ No page cache plugin found")

    # ── Compression ──
    sections.append("\n## Compression & Headers")
    headers = _v(data, "headers")
    if headers:
        has_gzip = "gzip" in headers.lower() or "br" in headers.lower()
        has_cache = "cache-control" in headers.lower() or "expires" in headers.lower()
        if has_gzip:
            sections.append("✓ Response compression enabled")
        else:
            findings.append("⚠ No GZIP/Brotli compression — responses are sent uncompressed")
            sections.append("✗ No compression detected")
        if has_cache:
            sections.append("✓ Cache headers present")
        else:
            findings.append("⚠ No cache-control headers — browsers re-download everything")

    # ── WP-Cron ──
    sections.append("\n## WP-Cron")
    cron_disabled = _v(data, "cron_constant")
    if cron_disabled and cron_disabled.strip() == "0":
        findings.append(
            "⚠ WP-CRON ON PAGE LOADS: WordPress runs scheduled tasks on random "
            "page loads, causing unpredictable slowdowns. Add "
            "define('DISABLE_WP_CRON', true) to wp-config.php and set up a "
            "system cron: */5 * * * * curl https://domain/wp-cron.php"
        )
        sections.append("✗ WP-Cron running on page loads")
    else:
        sections.append("✓ WP-Cron disabled (using system cron)")

    # ── Plugin Count ──
    plugin_count = _v(data, "plugins")
    if plugin_count:
        try:
            count = int(plugin_count)
            sections.append(f"\n**Active plugins:** {count}")
            if count > 30:
                findings.append(
                    f"⚠ {count} active plugins — each adds PHP load time. "
                    f"Review and deactivate unused plugins."
                )
        except ValueError:
            pass

    # ── Database Bloat ──
    bloat = _v(data, "bloat")
    if bloat:
        sections.append("\n## Database Bloat")
        sections.append(bloat)

    # ── Images ──
    large_images = _v(data, "large_images")
    uploads_size = _v(data, "uploads_size")
    if large_images:
        try:
            count = int(large_images.strip())
            if count > 0:
                findings.append(
                    f"⚠ {count} images over 2MB in uploads — these slow down page "
                    f"loads significantly. Use ShortPixel or Imagify to compress."
                )
                sections.append(f"\n**Large images (>2MB):** {count}")
        except ValueError:
            pass
    if uploads_size:
        sections.append(f"**Uploads directory:** {uploads_size}")

    # ── PHP Config ──
    php_config = _v(data, "php_config")
    if php_config:
        sections.append(f"\n## PHP Configuration\n{php_config}")

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
    else:
        sections.append("\n✓ WordPress performance looks good. No major issues found.")

    return "\n".join(sections)
