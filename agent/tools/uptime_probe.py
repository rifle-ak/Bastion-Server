"""HTTP uptime probing and endpoint monitoring.

Lightweight HTTP checks for response time, status code, SSL expiry,
content matching, and redirect chains. Designed for continuous
monitoring via ``bastion monitor --probes``.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from agent.inventory import Inventory
from agent.tools.base import BaseTool, ToolResult
from agent.tools.docker_tools import _run_on_server


class UptimeProbe(BaseTool):
    """HTTP endpoint probe — check response time, status, SSL, content."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "uptime_probe"

    @property
    def description(self) -> str:
        return (
            "Probe HTTP endpoints: response time, status code, SSL expiry, "
            "content match, and redirect chain. Check one or multiple URLs."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server to probe from.",
                },
                "urls": {
                    "type": "string",
                    "description": "Comma-separated URLs to probe.",
                },
                "expect_content": {
                    "type": "string",
                    "description": "String that must appear in the response body (optional).",
                },
            },
            "required": ["server", "urls"],
        }

    async def execute(
        self, *, server: str, urls: str, expect_content: str | None = None, **kwargs: Any,
    ) -> ToolResult:
        """Probe URLs and report status."""
        url_list = [u.strip() for u in urls.split(",") if u.strip()]

        tasks = {
            url: _probe_one(self._inventory, server, url, expect_content)
            for url in url_list
        }
        keys = list(tasks.keys())
        results = await asyncio.gather(*[tasks[k] for k in keys])
        probe_results = dict(zip(keys, results))

        return ToolResult(output=_format_probes(probe_results))


async def _probe_one(
    inventory: Inventory, server: str, url: str, expect: str | None,
) -> dict[str, Any]:
    """Probe a single URL."""
    result: dict[str, Any] = {"url": url}

    # Timing + status
    timing_cmd = (
        f"curl -sL --max-time 15 -o /dev/null -w "
        f"'code:%{{http_code}}|ttfb:%{{time_starttransfer}}|"
        f"total:%{{time_total}}|redirect:%{{num_redirects}}|"
        f"ssl_verify:%{{ssl_verify_result}}|"
        f"size:%{{size_download}}' '{url}'"
    )
    timing = await _run_on_server(inventory, server, timing_cmd)

    if timing.success and timing.output:
        for part in timing.output.split("|"):
            if ":" in part:
                k, v = part.split(":", 1)
                result[k] = v
    else:
        result["error"] = timing.error or "Connection failed"
        return result

    # SSL certificate expiry check
    if url.startswith("https"):
        domain = url.split("//")[1].split("/")[0].split(":")[0]
        ssl_cmd = (
            f"echo | openssl s_client -servername {domain} "
            f"-connect {domain}:443 2>/dev/null | "
            f"openssl x509 -noout -enddate 2>/dev/null"
        )
        ssl_result = await _run_on_server(inventory, server, ssl_cmd)
        if ssl_result.success:
            result["ssl_expiry"] = ssl_result.output.strip()

    # Content match
    if expect:
        body_cmd = f"curl -sL --max-time 10 '{url}'"
        body = await _run_on_server(inventory, server, body_cmd)
        if body.success:
            result["content_match"] = expect.lower() in body.output.lower()
        else:
            result["content_match"] = False

    return result


def _format_probes(results: dict[str, dict[str, Any]]) -> str:
    """Format probe results into a report."""
    lines: list[str] = ["# Uptime Probe Results\n"]
    all_ok = True

    for url, data in results.items():
        if "error" in data:
            lines.append(f"✗ **{url}**")
            lines.append(f"  Error: {data['error']}")
            all_ok = False
            continue

        code = data.get("code", "?")
        ttfb = data.get("ttfb", "?")
        total = data.get("total", "?")
        size = data.get("size", "?")
        redirects = data.get("redirect", "0")

        # Determine status
        try:
            code_int = int(code)
            if code_int >= 500:
                icon = "✗"
                all_ok = False
            elif code_int >= 400:
                icon = "⚠"
                all_ok = False
            else:
                icon = "✓"
        except ValueError:
            icon = "?"

        lines.append(f"{icon} **{url}**")
        lines.append(f"  HTTP {code} | TTFB: {ttfb}s | Total: {total}s | Size: {size}B")

        if redirects != "0":
            lines.append(f"  Redirects: {redirects}")

        # TTFB warning
        try:
            ttfb_val = float(ttfb)
            if ttfb_val > 3:
                lines.append(f"  ⚠ TTFB very slow: {ttfb_val:.2f}s")
                all_ok = False
            elif ttfb_val > 1:
                lines.append(f"  ⚠ TTFB slow: {ttfb_val:.2f}s")
        except ValueError:
            pass

        # SSL expiry
        ssl_expiry = data.get("ssl_expiry", "")
        if ssl_expiry:
            lines.append(f"  SSL: {ssl_expiry}")

        # Content match
        if "content_match" in data:
            if data["content_match"]:
                lines.append("  ✓ Content match: found")
            else:
                lines.append("  ✗ Content match: NOT FOUND")
                all_ok = False

        lines.append("")

    if all_ok:
        lines.append("---\n✓ All endpoints healthy")
    else:
        lines.append("---\n⚠ Some endpoints have issues")

    return "\n".join(lines)
