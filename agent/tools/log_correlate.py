"""Cross-server log correlation for incident investigation.

When something goes wrong, you need logs from multiple servers and
services at the same time window. This tool pulls logs from all
related servers in parallel and presents a unified timeline.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent.inventory import Inventory
from agent.tools.base import BaseTool, ToolResult
from agent.tools.docker_tools import _run_on_server


class LogCorrelate(BaseTool):
    """Pull logs from multiple servers around an incident timeframe."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "log_correlate"

    @property
    def description(self) -> str:
        return (
            "Cross-server log correlation. Pull logs from multiple servers "
            "and services within a time window to investigate an incident. "
            "Specify servers, time range, and optional keyword filter."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "servers": {
                    "type": "string",
                    "description": "Comma-separated server names, or 'all'.",
                },
                "since": {
                    "type": "string",
                    "description": "Time range: '1h', '30m', '2h', etc.",
                    "default": "1h",
                },
                "keyword": {
                    "type": "string",
                    "description": "Filter logs for this keyword (optional).",
                },
                "services": {
                    "type": "string",
                    "description": "Comma-separated services to check (optional, auto-detected).",
                },
            },
            "required": ["servers"],
        }

    async def execute(
        self,
        *,
        servers: str,
        since: str = "1h",
        keyword: str | None = None,
        services: str | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        """Pull and correlate logs across servers."""
        if servers == "all":
            server_list = self._inventory.server_names
        else:
            server_list = [s.strip() for s in servers.split(",")]

        service_list = [s.strip() for s in services.split(",")] if services else None

        # Build log commands for each server
        tasks: dict[str, Any] = {}
        for srv_name in server_list:
            try:
                srv_info = self._inventory.get_server(srv_name)
            except KeyError:
                continue

            # System journal
            tasks[f"{srv_name}:syslog"] = _run_on_server(
                self._inventory, srv_name,
                f"journalctl --no-pager -n 100 --since '{since} ago' 2>/dev/null",
            )

            # Service-specific logs
            target_services = service_list or srv_info.definition.services
            for svc in target_services:
                if svc == "docker":
                    continue
                tasks[f"{srv_name}:svc:{svc}"] = _run_on_server(
                    self._inventory, srv_name,
                    f"journalctl -u {svc} --no-pager -n 50 --since '{since} ago' 2>/dev/null",
                )

            # Docker container logs
            if "docker" in srv_info.definition.services:
                tasks[f"{srv_name}:docker"] = _run_on_server(
                    self._inventory, srv_name,
                    f"docker ps --format '{{{{.Names}}}}' 2>/dev/null",
                )

            # dmesg (kernel errors)
            tasks[f"{srv_name}:dmesg"] = _run_on_server(
                self._inventory, srv_name,
                f"dmesg -T --level=err,crit,alert,emerg --nopager 2>/dev/null",
            )

        # Run all in parallel
        keys = list(tasks.keys())
        results = await asyncio.gather(*[tasks[k] for k in keys])
        data = dict(zip(keys, results))

        # Phase 2: Pull Docker container logs if we found containers
        docker_tasks: dict[str, Any] = {}
        for key, result in data.items():
            if key.endswith(":docker") and result.success and result.output.strip():
                srv_name = key.split(":")[0]
                containers = result.output.strip().splitlines()
                for container in containers[:10]:
                    container = container.strip()
                    if container:
                        docker_tasks[f"{srv_name}:container:{container}"] = _run_on_server(
                            self._inventory, srv_name,
                            f"docker logs --since {since} --tail 50 {container} 2>&1",
                        )

        if docker_tasks:
            dk = list(docker_tasks.keys())
            dr = await asyncio.gather(*[docker_tasks[k] for k in dk])
            data.update(dict(zip(dk, dr)))

        # Build report
        return ToolResult(output=_build_correlation_report(data, keyword, since))


def _build_correlation_report(
    data: dict[str, ToolResult],
    keyword: str | None,
    since: str,
) -> str:
    """Build a correlated log report across servers."""
    sections: list[str] = [f"# Log Correlation (last {since})\n"]

    # Group by server
    servers: dict[str, list[tuple[str, str]]] = {}
    for key, result in sorted(data.items()):
        parts = key.split(":", 1)
        srv = parts[0]
        source = parts[1] if len(parts) > 1 else "unknown"

        if not result.success or not result.output.strip():
            continue

        logs = result.output.strip()

        # Apply keyword filter
        if keyword:
            filtered = [l for l in logs.splitlines() if keyword.lower() in l.lower()]
            if not filtered:
                continue
            logs = "\n".join(filtered)

        servers.setdefault(srv, []).append((source, logs))

    if not servers:
        return "No log entries found matching the criteria."

    error_summary: dict[str, int] = {}

    for srv, sources in servers.items():
        sections.append(f"## {srv}\n")

        for source, logs in sources:
            lines = logs.splitlines()

            # Count errors
            error_lines = [
                l for l in lines
                if any(kw in l.lower() for kw in ("error", "fatal", "crit", "fail", "panic", "oom"))
            ]
            warn_lines = [
                l for l in lines
                if any(kw in l.lower() for kw in ("warn", "timeout", "refused", "denied"))
            ]

            if error_lines or warn_lines:
                label = source.replace("svc:", "service: ").replace("container:", "container: ")
                sections.append(f"**{label}** ({len(error_lines)} errors, {len(warn_lines)} warnings)")

                for line in error_lines[-5:]:
                    sections.append(f"  ✗ {line[:200]}")
                    # Track error types
                    for kw in ("oom", "timeout", "refused", "denied", "fatal", "panic", "segfault"):
                        if kw in line.lower():
                            error_summary[kw] = error_summary.get(kw, 0) + 1

                for line in warn_lines[-3:]:
                    sections.append(f"  ⚠ {line[:200]}")

                sections.append("")

    # Cross-server summary
    if error_summary:
        sections.append("---\n## Cross-Server Summary\n")
        sections.append("**Error patterns found across servers:**")
        for pattern, count in sorted(error_summary.items(), key=lambda x: -x[1]):
            sections.append(f"  {pattern}: {count} occurrences")

        # Correlation hints
        if "oom" in error_summary and "timeout" in error_summary:
            sections.append(
                "\n**Correlation:** OOM kills and timeouts detected together — "
                "memory pressure is likely causing service disruptions."
            )
        if "refused" in error_summary:
            sections.append(
                "\n**Correlation:** Connection refused errors suggest a service "
                "went down. Check restart times above."
            )

    return "\n".join(sections)
