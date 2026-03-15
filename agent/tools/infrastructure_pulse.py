"""Infrastructure pulse — smart session opener.

Runs a fast, lightweight sweep of all servers on session start and
presents a concise summary of what matters right now: issues, warnings,
things that changed, and a quick health snapshot.

This is NOT a full health check — it's a quick pulse designed to run
in <10 seconds and give the operator situational awareness immediately.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from agent.inventory import Inventory
from agent.tools.base import BaseTool, ToolResult
from agent.tools.docker_tools import _run_on_server


class InfrastructurePulse(BaseTool):
    """Quick infrastructure pulse — what you need to know right now."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "infrastructure_pulse"

    @property
    def description(self) -> str:
        return (
            "Fast infrastructure pulse: quick health snapshot of all servers. "
            "Shows disk/memory/load warnings, container issues, SSL expiring "
            "soon, uptime anomalies, and recent problems. Run at session start "
            "to get situational awareness."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {},
            "required": [],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        """Run fast pulse across all servers."""
        start = time.monotonic()
        server_names = self._inventory.server_names
        tasks: dict[str, Any] = {}

        for srv in server_names:
            try:
                self._inventory.get_server(srv)
            except KeyError:
                continue

            # Minimal, fast checks per server (~3 commands each)
            tasks[f"{srv}:vitals"] = _run_on_server(
                self._inventory, srv,
                "echo \"UPTIME:$(uptime)\""
                " && echo \"DISK:$(df -h / --output=pcent | tail -1)\""
                " && echo \"MEM:$(free -m | awk '/^Mem:/{printf \"%d/%dMB (%.0f%%)\", $3, $2, $3/$2*100}')\""
                " && echo \"LOAD:$(cat /proc/loadavg | cut -d' ' -f1-3)\"",
            )
            tasks[f"{srv}:docker_issues"] = _run_on_server(
                self._inventory, srv,
                "docker ps -a --filter 'status=restarting' --filter 'status=exited' "
                "--format '{{.Names}}|{{.Status}}' 2>/dev/null || echo ''",
            )
            tasks[f"{srv}:dmesg_recent"] = _run_on_server(
                self._inventory, srv,
                "dmesg -T --level=err,crit,alert,emerg 2>/dev/null | tail -3 || echo ''",
            )

        keys = list(tasks.keys())
        results = await asyncio.gather(*[tasks[k] for k in keys])
        data = dict(zip(keys, results))

        elapsed = time.monotonic() - start
        return ToolResult(output=_build_pulse(server_names, data, elapsed))


def _build_pulse(servers: list[str], data: dict[str, ToolResult], elapsed: float) -> str:
    """Build the pulse report."""
    lines: list[str] = ["# Infrastructure Pulse\n"]
    issues: list[str] = []
    warnings: list[str] = []
    healthy_count = 0
    unreachable: list[str] = []

    for srv in servers:
        vitals_result = data.get(f"{srv}:vitals")
        docker_result = data.get(f"{srv}:docker_issues")
        dmesg_result = data.get(f"{srv}:dmesg_recent")

        if not vitals_result or not vitals_result.success:
            unreachable.append(srv)
            continue

        srv_issues: list[str] = []
        vitals = vitals_result.output

        # Parse vitals
        disk_pct = _extract_value(vitals, "DISK:")
        mem_info = _extract_value(vitals, "MEM:")
        load_info = _extract_value(vitals, "LOAD:")
        uptime_info = _extract_value(vitals, "UPTIME:")

        # Disk check
        if disk_pct:
            try:
                pct = int(disk_pct.strip().rstrip("%"))
                if pct >= 90:
                    srv_issues.append(f"✗ Disk {pct}% full")
                elif pct >= 80:
                    srv_issues.append(f"⚠ Disk {pct}%")
            except ValueError:
                pass

        # Memory check
        if mem_info and "%" in mem_info:
            try:
                pct_str = mem_info.split("(")[1].rstrip("%)")
                pct = int(float(pct_str))
                if pct >= 90:
                    srv_issues.append(f"✗ Memory {mem_info}")
                elif pct >= 80:
                    srv_issues.append(f"⚠ Memory {mem_info}")
            except (IndexError, ValueError):
                pass

        # Load check
        if load_info:
            try:
                load_1m = float(load_info.split()[0])
                if load_1m > 10:
                    srv_issues.append(f"⚠ Load {load_info}")
            except (IndexError, ValueError):
                pass

        # Uptime — flag recent reboots
        if uptime_info:
            up_lower = uptime_info.lower()
            if "min" in up_lower and "day" not in up_lower:
                srv_issues.append(f"⚠ Recently rebooted ({uptime_info.strip()})")

        # Docker issues
        if docker_result and docker_result.success and docker_result.output.strip():
            problem_containers = [
                l.strip() for l in docker_result.output.strip().splitlines()
                if l.strip()
            ]
            for pc in problem_containers[:3]:
                parts = pc.split("|")
                name = parts[0] if parts else pc
                status = parts[1] if len(parts) > 1 else "unknown"
                srv_issues.append(f"✗ Container {name}: {status}")

        # dmesg errors
        if dmesg_result and dmesg_result.success and dmesg_result.output.strip():
            dmesg_lines = [l for l in dmesg_result.output.strip().splitlines() if l.strip()]
            if dmesg_lines:
                has_oom = any("oom" in l.lower() or "out of memory" in l.lower() for l in dmesg_lines)
                if has_oom:
                    srv_issues.append("✗ OOM kills detected in kernel log")
                elif len(dmesg_lines) > 0:
                    srv_issues.append(f"⚠ {len(dmesg_lines)} kernel errors in dmesg")

        if srv_issues:
            for issue in srv_issues:
                if issue.startswith("✗"):
                    issues.append(f"**{srv}**: {issue}")
                else:
                    warnings.append(f"**{srv}**: {issue}")
        else:
            healthy_count += 1

    # Build output
    total = len(servers)
    problem_count = len(set(
        i.split("**")[1] for i in issues + warnings if "**" in i
    ))

    if unreachable:
        lines.append(f"✗ **{len(unreachable)} unreachable:** {', '.join(unreachable)}")
        lines.append("")

    if issues:
        lines.append("## Critical")
        for i in issues:
            lines.append(f"  {i}")
        lines.append("")

    if warnings:
        lines.append("## Warnings")
        for w in warnings:
            lines.append(f"  {w}")
        lines.append("")

    if not issues and not warnings and not unreachable:
        lines.append(f"✓ All {total} servers healthy. No issues detected.")
    else:
        lines.append(
            f"---\n{healthy_count}/{total} servers clean"
            f" | {len(issues)} critical | {len(warnings)} warnings"
            f" | {len(unreachable)} unreachable"
        )

    lines.append(f"\n_Pulse completed in {elapsed:.1f}s_")
    return "\n".join(lines)


def _extract_value(text: str, prefix: str) -> str:
    """Extract value after a prefix from multi-line output."""
    for line in text.splitlines():
        if prefix in line:
            return line.split(prefix, 1)[1].strip()
    return ""
