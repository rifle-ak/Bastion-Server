"""What-changed detector for incident investigation.

When something breaks, the #1 question is "what changed?"
This tool checks for recent changes across the infrastructure:
- Package updates
- Docker image pulls
- Config file modifications
- Service restarts
- Cron job changes
- Pterodactyl/Wings updates
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent.inventory import Inventory
from agent.tools.base import BaseTool, ToolResult
from agent.tools.docker_tools import _run_on_server


class WhatChanged(BaseTool):
    """Detect recent changes on a server — packages, configs, containers."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "what_changed"

    @property
    def description(self) -> str:
        return (
            "Detect what changed recently on a server: package updates, "
            "Docker image pulls, config file modifications, service restarts, "
            "new containers, cron changes. Essential for incident investigation."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server to investigate.",
                },
                "hours": {
                    "type": "integer",
                    "description": "How far back to look (hours). Default: 24.",
                    "default": 24,
                },
            },
            "required": ["server"],
        }

    async def execute(self, *, server: str, hours: int = 24, **kwargs: Any) -> ToolResult:
        """Check for recent changes."""
        checks: dict[str, Any] = {
            # Package updates (apt)
            "apt_history": _run_on_server(
                self._inventory, server,
                f"grep -h 'Upgrade\\|Install\\|Remove' /var/log/apt/history.log "
                f"/var/log/dpkg.log 2>/dev/null | tail -30",
            ),
            # Package updates (yum/dnf)
            "yum_history": _run_on_server(
                self._inventory, server,
                "yum history list 2>/dev/null | head -15 || "
                "dnf history list 2>/dev/null | head -15",
            ),
            # Recently modified config files
            "config_changes": _run_on_server(
                self._inventory, server,
                f"find /etc -maxdepth 3 -type f -mmin -{hours * 60} "
                f"-not -path '*/ssl/*' -not -name '*.dpkg-*' "
                f"2>/dev/null | head -20",
            ),
            # Docker: recently created/updated containers
            "docker_events": _run_on_server(
                self._inventory, server,
                f"docker events --since {hours}h --until 0s "
                f"--filter 'type=container' "
                f"--filter 'event=create' --filter 'event=destroy' "
                f"--filter 'event=start' --filter 'event=stop' "
                f"--filter 'event=restart' --filter 'event=die' "
                f"--format '{{{{.Time}}}} {{{{.Action}}}} {{{{.Actor.Attributes.name}}}}' "
                f"2>/dev/null | tail -30",
            ),
            # Docker: recently pulled images
            "docker_images": _run_on_server(
                self._inventory, server,
                f"docker images --format '{{{{.Repository}}}}:{{{{.Tag}}}}  {{{{.CreatedAt}}}}' "
                f"2>/dev/null | head -15",
            ),
            # Service state changes (systemd)
            "service_changes": _run_on_server(
                self._inventory, server,
                f"journalctl --no-pager -n 50 --since '{hours}h ago' "
                f"-t systemd 2>/dev/null | "
                f"grep -i 'start\\|stop\\|restart\\|fail' | tail -20",
            ),
            # Crontab modifications
            "cron_changes": _run_on_server(
                self._inventory, server,
                f"find /etc/cron.d /etc/crontab /var/spool/cron "
                f"-type f -mmin -{hours * 60} 2>/dev/null",
            ),
            # Login history
            "logins": _run_on_server(
                self._inventory, server,
                "last -20 2>/dev/null | head -15",
            ),
            # Pterodactyl Wings changes
            "wings_version": _run_on_server(
                self._inventory, server,
                "wings version 2>/dev/null",
            ),
            # Recently modified files in key directories
            "pterodactyl_changes": _run_on_server(
                self._inventory, server,
                f"find /etc/pterodactyl /srv/pterodactyl -type f "
                f"-mmin -{hours * 60} 2>/dev/null | head -10",
            ),
            # System reboots
            "reboots": _run_on_server(
                self._inventory, server,
                "last reboot 2>/dev/null | head -5",
            ),
        }

        keys = list(checks.keys())
        results = await asyncio.gather(*[checks[k] for k in keys])
        data = dict(zip(keys, results))

        return ToolResult(output=_build_changes_report(server, hours, data))


def _v(data: dict[str, ToolResult], key: str) -> str:
    r = data.get(key)
    return r.output.strip() if r and r.success else ""


def _build_changes_report(server: str, hours: int, data: dict[str, ToolResult]) -> str:
    """Build a what-changed report."""
    sections: list[str] = [f"# What Changed: {server} (last {hours}h)\n"]
    change_count = 0

    # ── Package Updates ──
    apt = _v(data, "apt_history")
    yum = _v(data, "yum_history")
    pkg_changes = apt or yum
    if pkg_changes:
        lines = [l for l in pkg_changes.splitlines() if l.strip()]
        if lines:
            change_count += len(lines)
            sections.append(f"## Package Changes ({len(lines)} entries)")
            for line in lines[-10:]:
                sections.append(f"  {line.strip()}")
            sections.append("")

    # ── Config File Modifications ──
    configs = _v(data, "config_changes")
    if configs:
        files = [l.strip() for l in configs.splitlines() if l.strip()]
        if files:
            change_count += len(files)
            sections.append(f"## Config Files Modified ({len(files)} files)")
            for f in files:
                sections.append(f"  {f}")
            sections.append("")

    # ── Docker Events ──
    docker_events = _v(data, "docker_events")
    if docker_events:
        events = [l.strip() for l in docker_events.splitlines() if l.strip()]
        if events:
            change_count += len(events)
            sections.append(f"## Docker Events ({len(events)} events)")
            for e in events[-15:]:
                sections.append(f"  {e}")
            sections.append("")

    # ── Docker Images ──
    images = _v(data, "docker_images")
    if images:
        sections.append("## Docker Images (recent)")
        for line in images.splitlines()[:10]:
            sections.append(f"  {line.strip()}")
        sections.append("")

    # ── Service Changes ──
    services = _v(data, "service_changes")
    if services:
        svc_lines = [l.strip() for l in services.splitlines() if l.strip()]
        if svc_lines:
            change_count += len(svc_lines)
            sections.append(f"## Service State Changes ({len(svc_lines)} events)")
            for line in svc_lines[-10:]:
                sections.append(f"  {line}")
            sections.append("")

    # ── Cron Changes ──
    cron = _v(data, "cron_changes")
    if cron:
        cron_files = [l.strip() for l in cron.splitlines() if l.strip()]
        if cron_files:
            change_count += len(cron_files)
            sections.append(f"## Cron Changes ({len(cron_files)} files modified)")
            for f in cron_files:
                sections.append(f"  {f}")
            sections.append("")

    # ── Pterodactyl Changes ──
    ptero = _v(data, "pterodactyl_changes")
    wings = _v(data, "wings_version")
    if ptero:
        ptero_files = [l.strip() for l in ptero.splitlines() if l.strip()]
        change_count += len(ptero_files)
        sections.append("## Pterodactyl Changes")
        if wings:
            sections.append(f"  Wings version: {wings}")
        for f in ptero_files:
            sections.append(f"  Modified: {f}")
        sections.append("")

    # ── Login History ──
    logins = _v(data, "logins")
    if logins:
        login_lines = [
            l.strip() for l in logins.splitlines()
            if l.strip() and "still logged in" not in l.lower() and "wtmp" not in l.lower()
        ]
        if login_lines:
            sections.append(f"## Recent Logins ({len(login_lines)} sessions)")
            for line in login_lines[:10]:
                sections.append(f"  {line}")
            sections.append("")

    # ── Reboots ──
    reboots = _v(data, "reboots")
    if reboots:
        reboot_lines = [l for l in reboots.splitlines() if "reboot" in l.lower()]
        if reboot_lines:
            change_count += len(reboot_lines)
            sections.append("## System Reboots")
            for line in reboot_lines:
                sections.append(f"  {line.strip()}")
            sections.append("")

    # ── Summary ──
    sections.append("---")
    if change_count > 0:
        sections.append(f"\n**{change_count} changes detected** in the last {hours} hours.")
        sections.append(
            "Correlate the timestamps above with when the issue started "
            "to identify the likely cause."
        )
    else:
        sections.append(f"\n✓ No significant changes detected in the last {hours} hours.")
        sections.append(
            "If an issue appeared recently, it may be caused by external "
            "factors (traffic spike, upstream provider, client actions)."
        )

    return "\n".join(sections)
