"""Resource rightsizing analysis tool.

Analyzes resource usage across servers and containers to identify
over-provisioned (wasting money) and under-provisioned (risking
performance) resources. Supports Pterodactyl game servers, cPanel/WHM
servers, and host-level analysis.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent.inventory import Inventory
from agent.tools.base import BaseTool, ToolResult
from agent.tools.docker_tools import _run_on_server


# Thresholds for classification
OVER_PROVISIONED_THRESHOLD = 30   # <30% usage = wasting money
UNDER_PROVISIONED_THRESHOLD = 85  # >85% usage = risking performance


def _parse_percentage(value: str) -> float | None:
    """Parse a percentage string like '45.2%' into a float."""
    try:
        return float(value.strip().rstrip("%"))
    except (ValueError, AttributeError):
        return None


def _parse_memory_bytes(value: str) -> int | None:
    """Parse memory strings like '1.5GiB', '512MiB', '2GB' into bytes."""
    value = value.strip()
    multipliers = {
        "B": 1, "KIB": 1024, "KB": 1000,
        "MIB": 1024**2, "MB": 1000**2,
        "GIB": 1024**3, "GB": 1000**3,
        "TIB": 1024**4, "TB": 1000**4,
    }
    for suffix, mult in sorted(multipliers.items(), key=lambda x: -len(x[0])):
        if value.upper().endswith(suffix):
            try:
                return int(float(value[:len(value) - len(suffix)]) * mult)
            except ValueError:
                return None
    try:
        return int(value)
    except ValueError:
        return None


def _format_bytes(num_bytes: int) -> str:
    """Format bytes into a human-readable string."""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(num_bytes) < 1024:
            return f"{num_bytes:.1f}{unit}"
        num_bytes /= 1024  # type: ignore[assignment]
    return f"{num_bytes:.1f}PiB"


def _classify_usage(pct: float) -> str:
    """Classify a usage percentage as over/under/right-sized."""
    if pct < OVER_PROVISIONED_THRESHOLD:
        return "over-provisioned"
    if pct > UNDER_PROVISIONED_THRESHOLD:
        return "under-provisioned"
    return "right-sized"


def _parse_docker_stats(output: str) -> list[dict[str, Any]]:
    """Parse docker stats output into structured data."""
    containers: list[dict[str, Any]] = []
    for line in output.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        name = parts[0].strip()
        mem_usage_raw = parts[1].strip()
        mem_pct = _parse_percentage(parts[2])
        cpu_pct = _parse_percentage(parts[3])
        # Parse "256MiB / 1GiB" format
        mem_used: int | None = None
        mem_limit: int | None = None
        if " / " in mem_usage_raw:
            used_str, limit_str = mem_usage_raw.split(" / ", 1)
            mem_used = _parse_memory_bytes(used_str)
            mem_limit = _parse_memory_bytes(limit_str)
        containers.append({
            "name": name,
            "mem_used": mem_used,
            "mem_limit": mem_limit,
            "mem_pct": mem_pct,
            "cpu_pct": cpu_pct,
        })
    return containers


def _parse_free_output(output: str) -> dict[str, Any]:
    """Parse 'free -b' output into structured data."""
    result: dict[str, Any] = {}
    for line in output.strip().splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].lower().startswith("mem"):
            result["ram_total"] = int(parts[1]) if parts[1].isdigit() else 0
            result["ram_used"] = int(parts[2]) if parts[2].isdigit() else 0
            if result["ram_total"] > 0:
                result["ram_pct"] = round(result["ram_used"] / result["ram_total"] * 100, 1)
        if len(parts) >= 3 and parts[0].lower().startswith("swap"):
            result["swap_total"] = int(parts[1]) if parts[1].isdigit() else 0
            result["swap_used"] = int(parts[2]) if parts[2].isdigit() else 0
    return result


def _parse_df_output(output: str) -> list[dict[str, Any]]:
    """Parse 'df -h' output into structured data."""
    disks: list[dict[str, Any]] = []
    for line in output.strip().splitlines()[1:]:  # Skip header
        parts = line.split()
        if len(parts) >= 5 and parts[4].endswith("%"):
            disks.append({
                "filesystem": parts[0],
                "size": parts[1],
                "used": parts[2],
                "available": parts[3],
                "use_pct": _parse_percentage(parts[4]),
                "mount": parts[5] if len(parts) > 5 else "",
            })
    return disks


def _parse_loadavg(uptime_output: str) -> list[float]:
    """Parse load averages from uptime output."""
    loads: list[float] = []
    if "load average:" in uptime_output:
        avg_part = uptime_output.split("load average:")[-1].strip()
        for val in avg_part.split(","):
            try:
                loads.append(float(val.strip()))
            except ValueError:
                pass
    return loads


def _parse_cpu_count(output: str) -> int | None:
    """Parse CPU count from nproc output."""
    try:
        return int(output.strip())
    except (ValueError, AttributeError):
        return None


def _parse_iostat(output: str) -> list[dict[str, Any]]:
    """Parse iostat -x output for disk IO stats."""
    devices: list[dict[str, Any]] = []
    for line in output.strip().splitlines():
        parts = line.split()
        if not parts or parts[0] in ("Linux", "Device", "Device:", "avg-cpu:"):
            continue
        # iostat device lines typically start with device name
        if len(parts) >= 7:
            try:
                float(parts[1])  # Verify second column is numeric
                util = _parse_percentage(parts[-1]) if parts[-1].endswith("%") else None
                if util is None:
                    try:
                        util = float(parts[-1])
                    except ValueError:
                        util = None
                devices.append({"device": parts[0], "util_pct": util})
            except ValueError:
                continue
    return devices


def _build_container_section(containers: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Build classified container lists for the report."""
    over: list[str] = []
    under: list[str] = []
    right: list[str] = []
    for c in containers:
        mem_pct = c.get("mem_pct")
        cpu_pct = c.get("cpu_pct")
        if mem_pct is None:
            continue
        label = c["name"]
        mem_info = f"RAM: {mem_pct:.1f}%"
        if c.get("mem_used") and c.get("mem_limit"):
            mem_info += f" ({_format_bytes(c['mem_used'])} / {_format_bytes(c['mem_limit'])})"
        if cpu_pct is not None:
            mem_info += f", CPU: {cpu_pct:.1f}%"
        classification = _classify_usage(mem_pct)
        entry = f"  - {label}: {mem_info}"
        if classification == "over-provisioned":
            over.append(entry)
        elif classification == "under-provisioned":
            under.append(entry)
        else:
            right.append(entry)
    return {"over": over, "under": under, "right": right}


def _build_host_section(data: dict[str, Any]) -> list[str]:
    """Build host-level analysis lines for the report."""
    lines: list[str] = []
    mem = data.get("memory", {})
    if mem.get("ram_total"):
        ram_pct = mem.get("ram_pct", 0)
        lines.append(f"  RAM: {ram_pct:.1f}% used "
                      f"({_format_bytes(mem['ram_used'])} / {_format_bytes(mem['ram_total'])})"
                      f" [{_classify_usage(ram_pct)}]")
    if mem.get("swap_used", 0) > 0:
        lines.append(f"  Swap: {_format_bytes(mem['swap_used'])} in use "
                      "(any swap = potential RAM under-provisioning)")
    loads = data.get("load_avg", [])
    cpu_count = data.get("cpu_count")
    if loads and cpu_count:
        load_ratio = loads[0] / cpu_count * 100
        lines.append(f"  CPU Load: {loads[0]:.2f} / {cpu_count} cores "
                      f"({load_ratio:.0f}%) [{_classify_usage(load_ratio)}]")
    for disk in data.get("disks", []):
        pct = disk.get("use_pct")
        if pct is not None:
            lines.append(f"  Disk {disk['mount']}: {pct:.0f}% used "
                          f"({disk['used']} / {disk['size']}) [{_classify_usage(pct)}]")
    return lines


def _build_recommendations(data: dict[str, Any]) -> list[str]:
    """Generate specific recommendations based on analysis data."""
    recs: list[str] = []
    mem = data.get("memory", {})
    if mem.get("swap_used", 0) > 0:
        recs.append("- Upgrade RAM: swap usage detected, server is memory-constrained")
    ram_pct = mem.get("ram_pct", 50)
    if ram_pct < OVER_PROVISIONED_THRESHOLD:
        recs.append(f"- Consider downgrading RAM: only {ram_pct:.0f}% used, could save on hosting costs")
    elif ram_pct > UNDER_PROVISIONED_THRESHOLD:
        recs.append(f"- Upgrade RAM urgently: {ram_pct:.0f}% used, risk of OOM kills")
    for c in data.get("containers", []):
        if c.get("mem_pct") is not None and c["mem_pct"] < OVER_PROVISIONED_THRESHOLD:
            if c.get("mem_limit"):
                suggested = max(c.get("mem_used", 0) * 2, 128 * 1024 * 1024)
                recs.append(f"- Reduce memory limit for '{c['name']}': "
                            f"using {c['mem_pct']:.0f}%, "
                            f"consider lowering to {_format_bytes(suggested)}")
        if c.get("mem_pct") is not None and c["mem_pct"] > UNDER_PROVISIONED_THRESHOLD:
            recs.append(f"- Increase memory for '{c['name']}': "
                        f"at {c['mem_pct']:.0f}%, risk of container OOM")
    for disk in data.get("disks", []):
        if disk.get("use_pct") is not None and disk["use_pct"] > UNDER_PROVISIONED_THRESHOLD:
            recs.append(f"- Disk {disk['mount']} at {disk['use_pct']:.0f}%: "
                        "expand storage or clean up old data")
    return recs


def _build_quick_wins(data: dict[str, Any]) -> list[str]:
    """Identify easy changes that save money or improve performance."""
    wins: list[str] = []
    over_containers = [c for c in data.get("containers", [])
                       if c.get("mem_pct") is not None and c["mem_pct"] < 15]
    if over_containers:
        names = ", ".join(c["name"] for c in over_containers)
        wins.append(f"- Containers using <15% RAM ({names}): "
                    "check if they are idle or can be consolidated")
    if data.get("lve_data"):
        wins.append("- Review CloudLinux LVE limits for accounts with low usage")
    if data.get("disk_quotas"):
        wins.append("- Review disk quotas for accounts with large unused allocations")
    mem = data.get("memory", {})
    if mem.get("ram_pct", 50) < 20:
        wins.append("- Server RAM utilization very low: consider migrating "
                    "workloads here or downsizing the server")
    loads = data.get("load_avg", [])
    cpu_count = data.get("cpu_count")
    if loads and cpu_count and (loads[0] / cpu_count) < 0.1:
        wins.append("- CPU nearly idle: server may be a candidate for consolidation")
    return wins


def _build_rightsizing_report(server: str, data: dict[str, Any]) -> str:
    """Build a complete rightsizing report from collected data.

    Args:
        server: Server name for the report header.
        data: Collected analysis data with keys: memory, disks, load_avg,
              cpu_count, containers, lve_data, disk_quotas, iostat.

    Returns:
        Formatted multi-section report string.
    """
    lines: list[str] = [f"# Resource Rightsizing Report: {server}", ""]

    # Server Overview
    lines.append("## Server Overview")
    host_lines = _build_host_section(data)
    lines.extend(host_lines if host_lines else ["  No host-level data available"])
    if data.get("iostat"):
        for dev in data["iostat"]:
            if dev.get("util_pct") is not None:
                lines.append(f"  IO {dev['device']}: {dev['util_pct']:.0f}% utilization")
    lines.append("")

    # Container/account analysis
    containers = data.get("containers", [])
    if containers:
        classified = _build_container_section(containers)
        if classified["over"]:
            lines.append("## Over-provisioned (wasting money, <30% usage)")
            lines.extend(classified["over"])
            lines.append("")
        if classified["under"]:
            lines.append("## Under-provisioned (risking performance, >85% usage)")
            lines.extend(classified["under"])
            lines.append("")
        if classified["right"]:
            lines.append("## Right-sized (30-85% usage)")
            lines.extend(classified["right"])
            lines.append("")

    # cPanel/WHM data
    if data.get("lve_data"):
        lines.append("## CloudLinux LVE Limits")
        lines.append(f"  {data['lve_data']}")
        lines.append("")
    if data.get("disk_quotas"):
        lines.append("## Disk Quotas")
        lines.append(f"  {data['disk_quotas']}")
        lines.append("")

    # Recommendations
    recs = _build_recommendations(data)
    lines.append("## Recommendations")
    lines.extend(recs if recs else ["  - All resources appear reasonably sized"])
    lines.append("")

    # Quick Wins
    wins = _build_quick_wins(data)
    lines.append("## Quick Wins")
    lines.extend(wins if wins else ["  - No immediate quick wins identified"])

    return "\n".join(lines)


class ResourceRightsizing(BaseTool):
    """Analyze resource usage to find over/under provisioned servers and containers."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "resource_rightsizing"

    @property
    def description(self) -> str:
        return (
            "Analyze resource usage to identify over-provisioned (wasting money) and "
            "under-provisioned (risking performance) servers and containers. "
            "Checks RAM, CPU, disk, swap, container limits, CloudLinux LVE, and disk quotas."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server name from the inventory.",
                },
                "focus": {
                    "type": "string",
                    "description": "Analysis focus: 'containers', 'accounts', 'host', or 'all' (default).",
                    "enum": ["containers", "accounts", "host", "all"],
                    "default": "all",
                },
            },
            "required": ["server"],
        }

    async def execute(
        self,
        *,
        server: str,
        focus: str = "all",
        **kwargs: Any,
    ) -> ToolResult:
        """Run rightsizing analysis on the target server."""
        try:
            self._inventory.get_server(server)
        except KeyError as e:
            return ToolResult(error=str(e), exit_code=1)

        data: dict[str, Any] = {}

        if focus in ("host", "all"):
            host_data = await self._collect_host_data(server)
            data.update(host_data)

        if focus in ("containers", "all"):
            container_data = await self._collect_container_data(server)
            data.update(container_data)

        if focus in ("accounts", "all"):
            account_data = await self._collect_account_data(server)
            data.update(account_data)

        report = _build_rightsizing_report(server, data)
        return ToolResult(output=report, exit_code=0)

    async def _collect_host_data(self, server: str) -> dict[str, Any]:
        """Collect host-level resource usage data in parallel."""
        commands = {
            "memory": "free -b",
            "disk": "df -h",
            "uptime": "uptime",
            "cpu_count": "nproc",
            "iostat": "iostat -x 1 1 2>/dev/null | tail -5",
        }
        results = await asyncio.gather(
            *[_run_on_server(self._inventory, server, cmd)
              for cmd in commands.values()]
        )
        result_map = dict(zip(commands.keys(), results))
        data: dict[str, Any] = {}
        mem_result = result_map["memory"]
        if mem_result.success:
            data["memory"] = _parse_free_output(mem_result.output)
        disk_result = result_map["disk"]
        if disk_result.success:
            data["disks"] = _parse_df_output(disk_result.output)
        uptime_result = result_map["uptime"]
        if uptime_result.success:
            data["load_avg"] = _parse_loadavg(uptime_result.output)
        cpu_result = result_map["cpu_count"]
        if cpu_result.success:
            data["cpu_count"] = _parse_cpu_count(cpu_result.output)
        iostat_result = result_map["iostat"]
        if iostat_result.success and iostat_result.output.strip():
            data["iostat"] = _parse_iostat(iostat_result.output)
        return data

    async def _collect_container_data(self, server: str) -> dict[str, Any]:
        """Collect Docker container resource usage data."""
        stats_cmd = (
            "docker stats --no-stream --format "
            "'{{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.CPUPerc}}'"
        )
        inspect_cmd = (
            "docker inspect --format "
            "'{{.Name}} {{.HostConfig.Memory}} {{.HostConfig.CpuQuota}}' "
            "$(docker ps -q) 2>/dev/null"
        )
        pterodactyl_cmd = "cat /etc/pterodactyl/config.yml 2>/dev/null"

        stats_r, inspect_r, ptero_r = await asyncio.gather(
            _run_on_server(self._inventory, server, stats_cmd),
            _run_on_server(self._inventory, server, inspect_cmd),
            _run_on_server(self._inventory, server, pterodactyl_cmd),
        )

        data: dict[str, Any] = {}
        if stats_r.success:
            data["containers"] = _parse_docker_stats(stats_r.output)
        else:
            data["containers"] = []

        if inspect_r.success and inspect_r.output.strip():
            data["container_inspect"] = inspect_r.output
        if ptero_r.success and ptero_r.output.strip():
            data["pterodactyl_config"] = ptero_r.output

        return data

    async def _collect_account_data(self, server: str) -> dict[str, Any]:
        """Collect cPanel/WHM account resource data."""
        lve_cmd = "lvectl list --json 2>/dev/null || lveinfo --json 2>/dev/null"
        quota_cmd = "repquota -a 2>/dev/null | head -30"

        lve_r, quota_r = await asyncio.gather(
            _run_on_server(self._inventory, server, lve_cmd),
            _run_on_server(self._inventory, server, quota_cmd),
        )

        data: dict[str, Any] = {}
        if lve_r.success and lve_r.output.strip():
            data["lve_data"] = lve_r.output
        if quota_r.success and quota_r.output.strip():
            data["disk_quotas"] = quota_r.output
        return data
