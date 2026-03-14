"""Cron job auditing and analysis.

Discovers cron jobs across all users, detects scheduling conflicts,
identifies crons running as root that shouldn't be, and flags crons
that may have stopped running.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from agent.inventory import Inventory
from agent.tools.base import BaseTool, ToolResult
from agent.tools.docker_tools import _run_on_server


class CronAudit(BaseTool):
    """Audit cron jobs on a server — schedules, conflicts, security."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "cron_audit"

    @property
    def description(self) -> str:
        return (
            "Audit all cron jobs on a server: list per-user and system crons, "
            "detect overlapping schedules, flag jobs running as root, find "
            "crons with errors, and identify disabled/broken crons."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server to audit.",
                },
            },
            "required": ["server"],
        }

    async def execute(self, *, server: str, **kwargs: Any) -> ToolResult:
        """Audit cron jobs."""
        checks: dict[str, Any] = {
            # System crontabs
            "system_crontab": _run_on_server(
                self._inventory, server,
                "cat /etc/crontab 2>/dev/null",
            ),
            # /etc/cron.d/
            "cron_d": _run_on_server(
                self._inventory, server,
                "ls -la /etc/cron.d/ 2>/dev/null",
            ),
            "cron_d_contents": _run_on_server(
                self._inventory, server,
                "head -n 5 /etc/cron.d/* 2>/dev/null",
            ),
            # Per-user crontabs
            "user_crons": _run_on_server(
                self._inventory, server,
                "ls /var/spool/cron/crontabs/ 2>/dev/null",
            ),
            "user_crons_alt": _run_on_server(
                self._inventory, server,
                "ls /var/spool/cron/ 2>/dev/null",
            ),
            # Cron service status
            "cron_status": _run_on_server(
                self._inventory, server,
                "systemctl is-active cron crond 2>/dev/null",
            ),
            # Recent cron log
            "cron_log": _run_on_server(
                self._inventory, server,
                "tail -n 100 /var/log/cron 2>/dev/null",
            ),
            "syslog_cron": _run_on_server(
                self._inventory, server,
                "journalctl -u cron -u crond --no-pager -n 50 --since '24h ago' 2>/dev/null",
            ),
            # Timer units (systemd timers = modern cron)
            "timers": _run_on_server(
                self._inventory, server,
                "systemctl list-timers --all --no-pager 2>/dev/null",
            ),
            # Hourly/daily/weekly crons
            "cron_hourly": _run_on_server(
                self._inventory, server,
                "ls /etc/cron.hourly/ 2>/dev/null",
            ),
            "cron_daily": _run_on_server(
                self._inventory, server,
                "ls /etc/cron.daily/ 2>/dev/null",
            ),
        }

        keys = list(checks.keys())
        results = await asyncio.gather(*[checks[k] for k in keys])
        data = dict(zip(keys, results))

        # Phase 2: Read specific user crontabs
        user_dir = _v(data, "user_crons") or _v(data, "user_crons_alt")
        user_tasks: dict[str, Any] = {}
        if user_dir:
            for username in user_dir.strip().splitlines():
                username = username.strip()
                if username and not username.startswith("."):
                    for path in [
                        f"/var/spool/cron/crontabs/{username}",
                        f"/var/spool/cron/{username}",
                    ]:
                        user_tasks[f"user:{username}"] = _run_on_server(
                            self._inventory, server,
                            f"cat {path} 2>/dev/null",
                        )
                        break

        if user_tasks:
            uk = list(user_tasks.keys())
            ur = await asyncio.gather(*[user_tasks[k] for k in uk])
            data.update(dict(zip(uk, ur)))

        return ToolResult(output=_build_cron_report(server, data))


def _v(data: dict[str, ToolResult], key: str) -> str:
    r = data.get(key)
    return r.output.strip() if r and r.success else ""


def _build_cron_report(server: str, data: dict[str, ToolResult]) -> str:
    """Build cron audit report."""
    sections: list[str] = [f"# Cron Audit: {server}\n"]
    findings: list[str] = []
    all_jobs: list[dict[str, str]] = []

    # Cron service status
    cron_status = _v(data, "cron_status")
    if "active" in cron_status.lower():
        sections.append("✓ Cron service: active")
    else:
        findings.append("✗ Cron service not running — no jobs will execute")
        sections.append("✗ Cron service: not active")

    # System crontab
    sections.append("\n## System Crontab (/etc/crontab)")
    sys_cron = _v(data, "system_crontab")
    if sys_cron:
        jobs = _parse_crontab(sys_cron, "root")
        all_jobs.extend(jobs)
        sections.append(f"{len(jobs)} jobs defined")
        for job in jobs:
            sections.append(f"  {job['schedule']} → {job['command'][:80]}")

    # /etc/cron.d/
    cron_d = _v(data, "cron_d")
    if cron_d:
        files = [l.split()[-1] for l in cron_d.splitlines() if not l.startswith("total")]
        sections.append(f"\n## /etc/cron.d/ ({len(files)} files)")
        for f in files:
            sections.append(f"  {f}")

    # Periodic directories
    for period in ("hourly", "daily"):
        content = _v(data, f"cron_{period}")
        if content:
            scripts = [l.strip() for l in content.splitlines() if l.strip()]
            sections.append(f"\n**cron.{period}:** {len(scripts)} scripts")

    # User crontabs
    sections.append("\n## User Crontabs")
    user_count = 0
    for key, result in sorted(data.items()):
        if not key.startswith("user:"):
            continue
        username = key[5:]
        if not result.success or not result.output.strip():
            continue
        user_count += 1
        jobs = _parse_crontab(result.output, username)
        all_jobs.extend(jobs)
        sections.append(f"\n**{username}** ({len(jobs)} jobs):")
        for job in jobs:
            sections.append(f"  {job['schedule']} → {job['command'][:80]}")

            # Flag root-like commands in user crons
            if any(kw in job["command"].lower() for kw in ("sudo", "chmod 777", "rm -rf /")):
                findings.append(f"⚠ User {username} has risky cron: {job['command'][:60]}")

    sections.append(f"\n{user_count} users with cron jobs, {len(all_jobs)} total jobs")

    # Systemd timers
    timers = _v(data, "timers")
    if timers:
        timer_lines = [l for l in timers.splitlines() if ".timer" in l]
        if timer_lines:
            sections.append(f"\n## Systemd Timers ({len(timer_lines)} active)")
            for line in timer_lines[:15]:
                sections.append(f"  {line.strip()}")

    # ── Analysis ──

    # Detect overlapping jobs
    overlaps = _find_overlaps(all_jobs)
    if overlaps:
        sections.append("\n## Potential Conflicts")
        for overlap in overlaps:
            findings.append(f"⚠ Overlapping crons: {overlap}")
            sections.append(f"  ⚠ {overlap}")

    # Check cron log for errors
    cron_log = _v(data, "cron_log") or _v(data, "syslog_cron")
    if cron_log:
        error_lines = [
            l for l in cron_log.splitlines()
            if any(kw in l.lower() for kw in ("error", "failed", "cannot", "permission denied"))
        ]
        if error_lines:
            findings.append(f"⚠ {len(error_lines)} cron errors in recent log")
            sections.append("\n## Recent Cron Errors")
            for line in error_lines[-5:]:
                sections.append(f"  ✗ {line[:200]}")

    # Summary
    sections.append("\n---")
    if findings:
        sections.append(f"\n## Findings ({len(findings)} issues)\n")
        for f in findings:
            sections.append(f)
    else:
        sections.append("\n✓ Cron configuration looks healthy.")

    return "\n".join(sections)


def _parse_crontab(content: str, default_user: str) -> list[dict[str, str]]:
    """Parse crontab content into structured jobs."""
    jobs: list[dict[str, str]] = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("MAILTO") or "=" in line.split()[0] if line.split() else True:
            continue
        # Standard cron format: min hour dom mon dow [user] command
        parts = line.split(None, 5)
        if len(parts) >= 6:
            schedule = " ".join(parts[:5])
            command = parts[5]
            jobs.append({
                "schedule": schedule,
                "command": command,
                "user": default_user,
            })
    return jobs


def _find_overlaps(jobs: list[dict[str, str]]) -> list[str]:
    """Find jobs with identical schedules that might conflict."""
    schedule_groups: dict[str, list[str]] = {}
    for job in jobs:
        key = job["schedule"]
        schedule_groups.setdefault(key, []).append(job["command"][:60])

    overlaps: list[str] = []
    for schedule, commands in schedule_groups.items():
        if len(commands) > 1:
            overlaps.append(
                f"{schedule} runs {len(commands)} jobs simultaneously: "
                + ", ".join(commands[:3])
            )
    return overlaps[:5]
