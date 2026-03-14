"""Batch WordPress security and update scanner.

Discovers all WordPress installations on a server and runs security
checks across all of them in parallel. Produces a single report
showing which sites need attention.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent.inventory import Inventory
from agent.tools.base import BaseTool, ToolResult
from agent.tools.docker_tools import _run_on_server


class WpScanAll(BaseTool):
    """Batch scan all WordPress installs on a server."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "wp_scan_all"

    @property
    def description(self) -> str:
        return (
            "Discover and scan ALL WordPress installations on a server. "
            "Checks core updates, plugin updates, security issues, and "
            "malware indicators across every install. One call = full audit."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server name (webhost role).",
                },
                "checks": {
                    "type": "string",
                    "description": (
                        "Comma-separated checks to run: 'updates', 'security', "
                        "'all' (default: 'all')."
                    ),
                    "default": "all",
                },
            },
            "required": ["server"],
        }

    async def execute(self, *, server: str, checks: str = "all", **kwargs: Any) -> ToolResult:
        """Discover WP installs and scan them all in parallel."""
        # Step 1: Find all wp-config.php files
        discover = await _run_on_server(
            self._inventory, server,
            "find /home/*/public_html -maxdepth 3 -name wp-config.php -type f 2>/dev/null",
        )
        if not discover.success:
            return discover

        wp_configs = [
            line.strip() for line in discover.output.strip().splitlines()
            if line.strip()
        ]

        if not wp_configs:
            return ToolResult(output="No WordPress installations found on this server.")

        # Step 2: Parse install paths and owners
        installs: list[dict[str, str]] = []
        for config_path in wp_configs:
            # /home/USERNAME/public_html[/subdir]/wp-config.php
            parts = config_path.split("/")
            if len(parts) >= 4 and parts[1] == "home":
                username = parts[2]
                wp_path = "/".join(parts[:-1])  # Remove wp-config.php
                installs.append({"user": username, "path": wp_path})

        if not installs:
            return ToolResult(output="Found wp-config.php files but could not parse paths.")

        do_updates = checks in ("all", "updates")
        do_security = checks in ("all", "security")

        # Step 3: Scan all installs in parallel
        tasks = [
            _scan_one_install(self._inventory, server, inst, do_updates, do_security)
            for inst in installs
        ]
        results = await asyncio.gather(*tasks)

        # Step 4: Build the report
        report_lines: list[str] = [
            f"# WordPress Scan: {len(installs)} installations found\n"
        ]

        issues_count = 0
        critical_count = 0

        for inst, result in zip(installs, results):
            user = inst["user"]
            path = inst["path"]
            report_lines.append(f"## {user} — {path}")

            if result.get("error"):
                report_lines.append(f"  ✗ Error: {result['error']}")
                issues_count += 1
                continue

            # Version
            ver = result.get("version", "?")
            report_lines.append(f"  Version: {ver}")

            # Core update
            if do_updates:
                core = result.get("core_update", "")
                if "success" in core.lower() or not core.strip():
                    report_lines.append("  Core: ✓ Up to date")
                else:
                    report_lines.append(f"  Core: ⚠ Update available — {core.strip()[:100]}")
                    issues_count += 1

                # Plugin updates
                plugin_updates = result.get("plugin_updates", [])
                if plugin_updates:
                    report_lines.append(f"  Plugins: ⚠ {len(plugin_updates)} need updates")
                    for pu in plugin_updates[:5]:
                        report_lines.append(f"    - {pu}")
                    issues_count += len(plugin_updates)
                else:
                    report_lines.append("  Plugins: ✓ All up to date")

            # Security
            if do_security:
                core_verify = result.get("core_verify", "")
                if "success" in core_verify.lower():
                    report_lines.append("  Core integrity: ✓ Verified")
                elif core_verify.strip():
                    report_lines.append(f"  Core integrity: ✗ MODIFIED")
                    critical_count += 1

                php_uploads = result.get("php_uploads", 0)
                if php_uploads:
                    report_lines.append(f"  ✗ PHP in uploads: {php_uploads} files (MALWARE RISK)")
                    critical_count += 1
                else:
                    report_lines.append("  Uploads: ✓ Clean")

                obfuscated = result.get("obfuscated", 0)
                if obfuscated:
                    report_lines.append(f"  ⚠ Obfuscated code: {obfuscated} files")
                    issues_count += 1

            report_lines.append("")

        # Summary
        summary: list[str] = ["\n---", "## Summary"]
        summary.append(f"Scanned: {len(installs)} WordPress installations")
        if critical_count:
            summary.append(f"✗ Critical issues: {critical_count}")
        if issues_count:
            summary.append(f"⚠ Issues needing attention: {issues_count}")
        if not critical_count and not issues_count:
            summary.append("✓ All installations look healthy")

        report_lines.extend(summary)
        return ToolResult(output="\n".join(report_lines))


async def _scan_one_install(
    inventory: Inventory,
    server: str,
    inst: dict[str, str],
    do_updates: bool,
    do_security: bool,
) -> dict[str, Any]:
    """Scan a single WordPress installation."""
    user = inst["user"]
    path = inst["path"]
    wp = f"runuser -u {user} -- wp --path={path}"
    result: dict[str, Any] = {}

    try:
        # Version (always)
        ver = await _run_on_server(inventory, server, f"{wp} core version")
        result["version"] = ver.output.strip() if ver.success else "?"

        checks: dict[str, Any] = {}

        if do_updates:
            checks["core_update"] = _run_on_server(
                inventory, server, f"{wp} core check-update 2>&1",
            )
            checks["plugin_list"] = _run_on_server(
                inventory, server, f"{wp} plugin list --format=csv 2>&1",
            )

        if do_security:
            checks["core_verify"] = _run_on_server(
                inventory, server, f"{wp} core verify-checksums 2>&1",
            )
            checks["php_uploads"] = _run_on_server(
                inventory, server,
                f"find {path}/wp-content/uploads -name '*.php' -o -name '*.phtml' 2>/dev/null",
            )
            checks["obfuscated"] = _run_on_server(
                inventory, server,
                f"grep -rlc 'base64_decode\\|eval(' {path}/wp-content/plugins/ {path}/wp-content/themes/ 2>/dev/null",
            )

        if checks:
            keys = list(checks.keys())
            raw = await asyncio.gather(*[checks[k] for k in keys])
            results_map = dict(zip(keys, raw))

            if "core_update" in results_map:
                r = results_map["core_update"]
                result["core_update"] = r.output if r.success else ""

            if "plugin_list" in results_map:
                r = results_map["plugin_list"]
                if r.success:
                    updates = [
                        line.split(",")[0]
                        for line in r.output.splitlines()
                        if "available" in line.lower()
                    ]
                    result["plugin_updates"] = updates
                else:
                    result["plugin_updates"] = []

            if "core_verify" in results_map:
                r = results_map["core_verify"]
                result["core_verify"] = r.output if r.success else ""

            if "php_uploads" in results_map:
                r = results_map["php_uploads"]
                count = len(r.output.strip().splitlines()) if r.success and r.output.strip() else 0
                result["php_uploads"] = count

            if "obfuscated" in results_map:
                r = results_map["obfuscated"]
                count = len(r.output.strip().splitlines()) if r.success and r.output.strip() else 0
                result["obfuscated"] = count

    except Exception as e:
        result["error"] = str(e)

    return result
