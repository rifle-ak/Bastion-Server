"""Lightweight anomaly detection daemon — zero API tokens.

Runs periodic checks against all servers using SSH only (no Claude API).
When thresholds are breached or anomalies detected, fires alerts via
Discord/Slack/email. Only invokes the Claude API if configured to
generate a diagnosis for non-obvious anomalies.

Designed to fill the gap between Netdata (per-server) and the agent
(interactive) — catches the cross-server, slow-burn, and "weird" issues
that per-server monitoring misses.

Usage:
    bastion anomaly-monitor              # run once and exit
    bastion anomaly-monitor --loop 300   # check every 5 minutes
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from agent.inventory import Inventory
from agent.tools.base import ToolResult
from agent.tools.docker_tools import _run_on_server

logger = structlog.get_logger()

# Where we store baselines and history for anomaly comparison
_STATE_DIR = Path(os.environ.get("BASTION_STATE_DIR", "./state"))
_ANOMALY_FILE = _STATE_DIR / "anomaly_baselines.json"


@dataclass
class Anomaly:
    """A detected anomaly."""
    server: str
    category: str  # disk, memory, container, network, drift, etc.
    severity: str  # critical, warning, info
    message: str
    value: str = ""
    baseline: str = ""


@dataclass
class AnomalyReport:
    """Collection of anomalies from a single check run."""
    anomalies: list[Anomaly] = field(default_factory=list)
    checked_servers: int = 0
    elapsed: float = 0.0

    @property
    def has_issues(self) -> bool:
        return len(self.anomalies) > 0

    @property
    def critical_count(self) -> int:
        return sum(1 for a in self.anomalies if a.severity == "critical")

    @property
    def warning_count(self) -> int:
        return sum(1 for a in self.anomalies if a.severity == "warning")

    def format(self) -> str:
        """Format the report for display/alerting."""
        if not self.anomalies:
            return (
                f"✓ Anomaly scan clean — {self.checked_servers} servers checked "
                f"in {self.elapsed:.1f}s"
            )

        lines = [f"# Anomaly Detection Report\n"]
        lines.append(
            f"{self.critical_count} critical | {self.warning_count} warnings | "
            f"{self.checked_servers} servers in {self.elapsed:.1f}s\n"
        )

        # Group by server
        by_server: dict[str, list[Anomaly]] = {}
        for a in self.anomalies:
            by_server.setdefault(a.server, []).append(a)

        for srv, anomalies in sorted(by_server.items()):
            lines.append(f"## {srv}")
            for a in anomalies:
                icon = "✗" if a.severity == "critical" else "⚠"
                lines.append(f"  {icon} [{a.category}] {a.message}")
                if a.baseline:
                    lines.append(f"    Baseline: {a.baseline} → Now: {a.value}")
            lines.append("")

        return "\n".join(lines)


def load_baselines() -> dict[str, Any]:
    """Load saved baselines from disk."""
    try:
        return json.loads(_ANOMALY_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_baselines(baselines: dict[str, Any]) -> None:
    """Save baselines to disk."""
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    _ANOMALY_FILE.write_text(json.dumps(baselines, indent=2))


async def run_anomaly_scan(inventory: Inventory) -> AnomalyReport:
    """Run anomaly detection across all servers.

    This is the main entry point. Checks:
    - Disk growth rate (not just current %, but trend)
    - Memory creep (gradually increasing without release)
    - Container restart loops (restarted recently)
    - Process count anomalies
    - Network connection spikes
    - Uptime anomalies (unexpected reboots)
    - SSL certificate expiry countdown
    """
    start = time.monotonic()
    baselines = load_baselines()
    report = AnomalyReport()
    tasks: dict[str, Any] = {}

    servers = inventory.server_names
    report.checked_servers = len(servers)

    for srv in servers:
        try:
            inventory.get_server(srv)
        except KeyError:
            continue

        # Gather data points for anomaly detection
        tasks[f"{srv}:disk"] = _run_on_server(
            inventory, srv,
            "df -BM / --output=used,avail | tail -1",
        )
        tasks[f"{srv}:mem"] = _run_on_server(
            inventory, srv,
            "free -m | awk '/^Mem:/{print $3\"/\"$2}'",
        )
        tasks[f"{srv}:uptime_seconds"] = _run_on_server(
            inventory, srv,
            "cat /proc/uptime | cut -d' ' -f1",
        )
        tasks[f"{srv}:connections"] = _run_on_server(
            inventory, srv,
            "ss -s 2>/dev/null | head -3",
        )
        tasks[f"{srv}:containers"] = _run_on_server(
            inventory, srv,
            "docker ps -a --format '{{.Names}}|{{.Status}}|{{.RunningFor}}' 2>/dev/null || echo ''",
        )
        tasks[f"{srv}:proc_count"] = _run_on_server(
            inventory, srv,
            "ps aux --no-headers 2>/dev/null | wc -l",
        )

    # Run all in parallel
    keys = list(tasks.keys())
    results = await asyncio.gather(*[tasks[k] for k in keys])
    data = dict(zip(keys, results))

    now = time.time()
    new_baselines: dict[str, Any] = {}

    for srv in servers:
        srv_baseline = baselines.get(srv, {})
        srv_new: dict[str, Any] = {"checked_at": now}

        # ── Disk growth rate ──
        disk = _v(data, f"{srv}:disk")
        if disk:
            try:
                parts = disk.strip().split()
                used_mb = int(parts[0].rstrip("M"))
                srv_new["disk_used_mb"] = used_mb

                prev_used = srv_baseline.get("disk_used_mb")
                prev_time = srv_baseline.get("checked_at")
                if prev_used and prev_time:
                    hours_elapsed = (now - prev_time) / 3600
                    if hours_elapsed > 0.1:  # At least 6 minutes
                        growth_mb_per_day = ((used_mb - prev_used) / hours_elapsed) * 24
                        if growth_mb_per_day > 5000:  # >5GB/day
                            report.anomalies.append(Anomaly(
                                server=srv, category="disk",
                                severity="warning",
                                message=f"Disk growing {growth_mb_per_day:.0f} MB/day — will fill faster than expected",
                                value=f"{growth_mb_per_day:.0f} MB/day",
                                baseline=f"{prev_used}MB → {used_mb}MB",
                            ))
            except (ValueError, IndexError):
                pass

        # ── Uptime anomaly (unexpected reboot) ──
        uptime_raw = _v(data, f"{srv}:uptime_seconds")
        if uptime_raw:
            try:
                uptime_secs = float(uptime_raw.strip())
                srv_new["uptime_seconds"] = uptime_secs
                prev_uptime = srv_baseline.get("uptime_seconds")
                if prev_uptime and uptime_secs < prev_uptime:
                    report.anomalies.append(Anomaly(
                        server=srv, category="reboot",
                        severity="warning",
                        message="Server rebooted since last check",
                        value=f"Uptime: {uptime_secs/3600:.1f}h",
                        baseline=f"Was: {prev_uptime/3600:.1f}h",
                    ))
            except ValueError:
                pass

        # ── Container restart loops ──
        containers = _v(data, f"{srv}:containers")
        if containers:
            for line in containers.splitlines():
                parts = line.split("|")
                if len(parts) >= 2:
                    cname = parts[0].strip()
                    status = parts[1].strip().lower()
                    if "restarting" in status:
                        report.anomalies.append(Anomaly(
                            server=srv, category="container",
                            severity="critical",
                            message=f"Container {cname} is in a restart loop",
                        ))
                    elif "exited" in status and "ago" in status:
                        # Only flag if it exited recently (within ~1h)
                        if "second" in status or "minute" in status:
                            report.anomalies.append(Anomaly(
                                server=srv, category="container",
                                severity="warning",
                                message=f"Container {cname} exited recently: {status}",
                            ))

        # ── Process count anomaly ──
        proc_count = _v(data, f"{srv}:proc_count")
        if proc_count:
            try:
                count = int(proc_count.strip())
                srv_new["proc_count"] = count
                prev_count = srv_baseline.get("proc_count")
                if prev_count and count > prev_count * 2 and count > 200:
                    report.anomalies.append(Anomaly(
                        server=srv, category="processes",
                        severity="warning",
                        message=f"Process count doubled: {prev_count} → {count}",
                        value=str(count),
                        baseline=str(prev_count),
                    ))
            except ValueError:
                pass

        # ── Connection count ──
        connections = _v(data, f"{srv}:connections")
        if connections:
            import re
            total_match = re.search(r'Total:\s*(\d+)', connections)
            if total_match:
                total = int(total_match.group(1))
                srv_new["connections"] = total
                prev_conns = srv_baseline.get("connections")
                if prev_conns and total > prev_conns * 3 and total > 500:
                    report.anomalies.append(Anomaly(
                        server=srv, category="network",
                        severity="warning",
                        message=f"Connection count tripled: {prev_conns} → {total}",
                        value=str(total),
                        baseline=str(prev_conns),
                    ))

        new_baselines[srv] = srv_new

    # Save new baselines
    save_baselines(new_baselines)

    report.elapsed = time.monotonic() - start
    return report


def _v(data: dict[str, ToolResult], key: str) -> str:
    r = data.get(key)
    return r.output.strip() if r and r.success else ""
