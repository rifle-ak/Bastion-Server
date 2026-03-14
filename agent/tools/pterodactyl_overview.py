"""Cross-server Pterodactyl dashboard.

Sweeps all game-server nodes in the inventory in parallel, aggregates
container states, resource usage, Wings daemon health, and disk
pressure into a single operational overview. Designed to give an
operator an instant read on the entire fleet.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from agent.inventory import Inventory
from agent.tools.base import BaseTool, ToolResult
from agent.tools.docker_tools import _run_on_server


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RAM_WARN_THRESHOLD = 80.0   # Node-level: flag when >80 % RAM or disk used
_DISK_WARN_THRESHOLD = 80.0
_CONTAINER_RAM_ALERT = 90.0  # Per-container: flag when >90 % of its limit

# Commands executed on each Wings node (all read-only).
_CHECKS: dict[str, str] = {
    "docker_ps": (
        "docker ps -a --format '{{.Names}}|{{.Status}}|{{.Image}}'"
    ),
    "wings_config": (
        "cat /etc/pterodactyl/config.yml 2>/dev/null "
        "| grep -E 'sftp:|port:|remote:' | head -10"
    ),
    "wings_service": (
        "systemctl is-active wings 2>/dev/null "
        "|| systemctl is-active pteroq 2>/dev/null "
        "|| echo inactive"
    ),
    "disk": (
        "df -h /srv/pterodactyl/ 2>/dev/null "
        "|| df -h /var/lib/pterodactyl/ 2>/dev/null "
        "|| df -h /"
    ),
    "docker_stats": (
        "docker stats --no-stream "
        "--format '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}'"
    ),
    "restarting": (
        "docker ps -a --filter 'status=restarting' "
        "--format '{{.Names}}|{{.Status}}|{{.Image}}'"
    ),
}


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_docker_ps(raw: str) -> list[dict[str, str]]:
    """Parse ``docker ps -a --format 'Name|Status|Image'`` output."""
    containers: list[dict[str, str]] = []
    for line in raw.strip().splitlines():
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        name, status, image = (p.strip() for p in parts)
        containers.append({"name": name, "status": status, "image": image})
    return containers


def _classify_status(status_str: str) -> str:
    """Classify a ``docker ps`` Status string into running/stopped/errored."""
    lower = status_str.lower()
    if "up" in lower:
        return "running"
    if "restarting" in lower:
        return "errored"
    return "stopped"


def _recently_restarted(status_str: str) -> bool:
    """Return True if the container started within the last ~1 hour.

    Docker shows e.g. ``Up 5 minutes``, ``Up 30 seconds``,
    ``Up About an hour``.  We flag anything under 1 hour.
    """
    lower = status_str.lower()
    if "up" not in lower:
        return False
    if "second" in lower or "minute" in lower:
        return True
    if "about an hour" in lower:
        return True
    return False


_PERCENT_RE = re.compile(r"([\d.]+)%")


def _parse_docker_stats(
    raw: str,
) -> list[dict[str, str]]:
    """Parse ``docker stats --no-stream`` tab-separated output.

    Returns dicts with keys: name, cpu, mem_usage, mem_pct.
    """
    entries: list[dict[str, str]] = []
    for line in raw.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        entries.append({
            "name": parts[0].strip(),
            "cpu": parts[1].strip(),
            "mem_usage": parts[2].strip(),
            "mem_pct": parts[3].strip(),
        })
    return entries


def _parse_disk_usage(raw: str) -> dict[str, str] | None:
    """Extract Use% and available space from ``df -h`` output.

    Returns dict with keys: filesystem, size, used, avail, use_pct.
    """
    for line in raw.strip().splitlines():
        if line.startswith("Filesystem"):
            continue
        fields = line.split()
        if len(fields) >= 5:
            return {
                "filesystem": fields[0],
                "size": fields[1],
                "used": fields[2],
                "avail": fields[3],
                "use_pct": fields[4].rstrip("%"),
            }
    return None


def _pct_value(pct_str: str) -> float:
    """Extract a float from a percent string like ``45.2%`` or ``45``."""
    m = _PERCENT_RE.search(pct_str)
    return float(m.group(1)) if m else 0.0


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def _build_overview_report(
    results: dict[str, dict[str, ToolResult]],
) -> str:
    """Aggregate per-node check results into a human-readable dashboard.

    Args:
        results: Mapping of server name -> {check_name: ToolResult}.

    Returns:
        Formatted multi-section report string.
    """
    # Accumulators
    total_containers = 0
    running = 0
    stopped = 0
    errored = 0
    nodes_at_capacity: list[str] = []
    high_resource_containers: list[str] = []
    recently_restarted_list: list[str] = []
    restart_loop_containers: list[str] = []
    wings_down_nodes: list[str] = []
    disk_critical_nodes: list[str] = []
    node_sections: list[str] = []

    for node, checks in results.items():
        section_lines: list[str] = []
        section_lines.append(f"## {node}")

        # --- Wings service status ---
        wings_result = checks.get("wings_service")
        wings_status = "unknown"
        if wings_result and wings_result.success:
            wings_status = wings_result.output.strip().splitlines()[-1].strip()
        if wings_status != "active":
            wings_down_nodes.append(f"{node} ({wings_status})")
        section_lines.append(f"  Wings service: {wings_status}")

        # --- Wings config snippet ---
        cfg_result = checks.get("wings_config")
        if cfg_result and cfg_result.output.strip():
            section_lines.append(f"  Wings config: {cfg_result.output.strip()}")

        # --- Disk usage ---
        disk_result = checks.get("disk")
        disk_info = None
        if disk_result and disk_result.success:
            disk_info = _parse_disk_usage(disk_result.output)
        if disk_info:
            use_pct = float(disk_info["use_pct"]) if disk_info["use_pct"] else 0
            marker = " ** CRITICAL **" if use_pct >= _DISK_WARN_THRESHOLD else ""
            section_lines.append(
                f"  Disk: {disk_info['used']}/{disk_info['size']} "
                f"({disk_info['use_pct']}% used, {disk_info['avail']} free){marker}"
            )
            if use_pct >= _DISK_WARN_THRESHOLD:
                disk_critical_nodes.append(
                    f"{node} ({disk_info['use_pct']}% used, {disk_info['avail']} free)"
                )
        else:
            section_lines.append("  Disk: unable to determine")

        # --- Container list ---
        ps_result = checks.get("docker_ps")
        containers: list[dict[str, str]] = []
        if ps_result and ps_result.success:
            containers = _parse_docker_ps(ps_result.output)

        node_total = len(containers)
        node_running = 0
        node_stopped = 0
        node_errored = 0
        for c in containers:
            cls = _classify_status(c["status"])
            if cls == "running":
                node_running += 1
            elif cls == "errored":
                node_errored += 1
            else:
                node_stopped += 1

            if _recently_restarted(c["status"]):
                recently_restarted_list.append(f"{node}/{c['name']} ({c['status']})")

        total_containers += node_total
        running += node_running
        stopped += node_stopped
        errored += node_errored

        section_lines.append(
            f"  Containers: {node_total} total "
            f"({node_running} running, {node_stopped} stopped, {node_errored} errored)"
        )

        # --- Restart loops ---
        loop_result = checks.get("restarting")
        if loop_result and loop_result.success and loop_result.output.strip():
            for c in _parse_docker_ps(loop_result.output):
                restart_loop_containers.append(
                    f"{node}/{c['name']} (image: {c['image']})"
                )

        # --- Per-container resource usage ---
        stats_result = checks.get("docker_stats")
        stats_entries: list[dict[str, str]] = []
        if stats_result and stats_result.success:
            stats_entries = _parse_docker_stats(stats_result.output)

        node_ram_exceeded = False
        if stats_entries:
            section_lines.append("  Resource usage:")
            for entry in stats_entries:
                mem_pct = _pct_value(entry["mem_pct"])
                cpu_pct = _pct_value(entry["cpu"])
                alert = ""
                if mem_pct >= _CONTAINER_RAM_ALERT:
                    alert = " ** HIGH MEM **"
                    high_resource_containers.append(
                        f"{node}/{entry['name']} "
                        f"(CPU {entry['cpu']}, MEM {entry['mem_usage']} / {entry['mem_pct']})"
                    )
                if mem_pct >= _RAM_WARN_THRESHOLD:
                    node_ram_exceeded = True
                section_lines.append(
                    f"    {entry['name']}: "
                    f"CPU {entry['cpu']}, MEM {entry['mem_usage']} ({entry['mem_pct']}){alert}"
                )

        # Check node-level capacity
        if node_ram_exceeded or (disk_info and float(disk_info.get("use_pct", 0)) >= _DISK_WARN_THRESHOLD):
            reason_parts: list[str] = []
            if node_ram_exceeded:
                reason_parts.append("RAM")
            if disk_info and float(disk_info.get("use_pct", 0)) >= _DISK_WARN_THRESHOLD:
                reason_parts.append("disk")
            nodes_at_capacity.append(f"{node} ({', '.join(reason_parts)})")

        node_sections.append("\n".join(section_lines))

    # --- Build aggregate summary ---
    report_parts: list[str] = []
    report_parts.append("=" * 60)
    report_parts.append("  PTERODACTYL FLEET OVERVIEW")
    report_parts.append("=" * 60)
    report_parts.append("")

    report_parts.append(f"Nodes scanned: {len(results)}")
    report_parts.append(
        f"Total containers: {total_containers} "
        f"({running} running, {stopped} stopped, {errored} errored)"
    )
    report_parts.append("")

    # --- Problems section ---
    problems: list[str] = []

    if wings_down_nodes:
        problems.append("WINGS SERVICE DOWN:")
        for entry in wings_down_nodes:
            problems.append(f"  - {entry}")

    if disk_critical_nodes:
        problems.append("DISK SPACE CRITICAL (>= 80%):")
        for entry in disk_critical_nodes:
            problems.append(f"  - {entry}")

    if restart_loop_containers:
        problems.append("CONTAINERS IN RESTART LOOP:")
        for entry in restart_loop_containers:
            problems.append(f"  - {entry}")

    if high_resource_containers:
        problems.append("HIGH MEMORY USAGE (>= 90%):")
        for entry in high_resource_containers:
            problems.append(f"  - {entry}")

    if nodes_at_capacity:
        problems.append("NODES AT CAPACITY (>= 80% RAM or disk):")
        for entry in nodes_at_capacity:
            problems.append(f"  - {entry}")

    if recently_restarted_list:
        problems.append("RECENTLY RESTARTED (within ~1 hour):")
        for entry in recently_restarted_list:
            problems.append(f"  - {entry}")

    if problems:
        report_parts.append("-" * 40)
        report_parts.append("  PROBLEMS DETECTED")
        report_parts.append("-" * 40)
        report_parts.extend(problems)
        report_parts.append("")
    else:
        report_parts.append("No problems detected. All systems nominal.")
        report_parts.append("")

    # --- Per-node detail ---
    report_parts.append("-" * 40)
    report_parts.append("  PER-NODE DETAIL")
    report_parts.append("-" * 40)
    report_parts.append("")
    report_parts.append("\n\n".join(node_sections))

    return "\n".join(report_parts)


# ---------------------------------------------------------------------------
# Tool class
# ---------------------------------------------------------------------------

class PterodactylOverview(BaseTool):
    """Cross-server Pterodactyl dashboard aggregating all Wings nodes."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "pterodactyl_overview"

    @property
    def description(self) -> str:
        return (
            "Scan ALL game-server nodes and build an aggregated Pterodactyl "
            "fleet dashboard. Shows per-node container counts, resource usage, "
            "Wings daemon health, disk pressure, restart loops, and flags "
            "problems automatically. No parameters required."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {},
            "required": [],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        """Sweep all game-server nodes and return an aggregated report."""
        game_servers = self._inventory.get_servers_by_role("game-server")
        if not game_servers:
            return ToolResult(
                output="No servers with role 'game-server' found in inventory.",
                exit_code=0,
            )

        # Run all checks on all nodes in parallel.
        node_names = [s.name for s in game_servers]
        gather_tasks = [
            self._collect_node_data(server_name) for server_name in node_names
        ]
        node_results = await asyncio.gather(*gather_tasks, return_exceptions=True)

        # Pair results with node names, handling exceptions gracefully.
        results: dict[str, dict[str, ToolResult]] = {}
        for server_name, result in zip(node_names, node_results):
            if isinstance(result, Exception):
                results[server_name] = {
                    check: ToolResult(
                        error=f"Node unreachable: {result}", exit_code=1
                    )
                    for check in _CHECKS
                }
            else:
                results[server_name] = result

        report = _build_overview_report(results)
        return ToolResult(output=report, exit_code=0)

    async def _collect_node_data(
        self, server_name: str
    ) -> dict[str, ToolResult]:
        """Run all check commands on a single node in parallel.

        Args:
            server_name: Inventory name of the game-server node.

        Returns:
            Dict mapping check name to its ToolResult.
        """
        check_names = list(_CHECKS.keys())
        commands = list(_CHECKS.values())

        tasks = [
            _run_on_server(self._inventory, server_name, cmd)
            for cmd in commands
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        collected: dict[str, ToolResult] = {}
        for check_name, result in zip(check_names, results):
            if isinstance(result, Exception):
                collected[check_name] = ToolResult(
                    error=str(result), exit_code=1
                )
            else:
                collected[check_name] = result

        return collected
