"""Blast radius preview for destructive operations.

Before restarting a service, stopping a container, or rebooting a
server, this tool shows exactly what will be affected: how many
containers, active players, websites, services, and dependent systems.

Prevents "oops, I didn't know 50 players were connected" moments.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from agent.inventory import Inventory
from agent.tools.base import BaseTool, ToolResult
from agent.tools.docker_tools import _run_on_server


class BlastRadius(BaseTool):
    """Preview the impact of a destructive action before executing it."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "blast_radius"

    @property
    def description(self) -> str:
        return (
            "Preview the impact of a destructive action before executing. "
            "Shows: affected containers, active connections, dependent "
            "services, estimated player/visitor count. Use before any "
            "restart, stop, or reboot operation."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server where the action will happen.",
                },
                "action": {
                    "type": "string",
                    "description": (
                        "What you plan to do: 'restart docker', "
                        "'restart container X', 'restart nginx', "
                        "'reboot', 'stop container X', 'restart mysql', etc."
                    ),
                },
            },
            "required": ["server", "action"],
        }

    async def execute(self, *, server: str, action: str, **kwargs: Any) -> ToolResult:
        """Assess blast radius of a planned action."""
        action_lower = action.lower()

        checks: dict[str, Any] = {
            # Current containers and their status
            "containers": _run_on_server(
                self._inventory, server,
                "docker ps --format '{{.Names}}|{{.Status}}|{{.Ports}}' 2>/dev/null",
            ),
            # Active network connections (proxy for users/players)
            "connections": _run_on_server(
                self._inventory, server,
                "ss -tn state established 2>/dev/null | wc -l",
            ),
            # Per-port connection counts
            "port_connections": _run_on_server(
                self._inventory, server,
                "ss -tn state established 2>/dev/null | "
                "awk '{print $4}' | rev | cut -d: -f1 | rev | "
                "sort | uniq -c | sort -rn | head -15",
            ),
            # Running services
            "services": _run_on_server(
                self._inventory, server,
                "systemctl list-units --type=service --state=running --no-pager --no-legend 2>/dev/null | head -30",
            ),
            # Server uptime
            "uptime": _run_on_server(
                self._inventory, server, "uptime",
            ),
        }

        # If restarting a specific container, get its connections
        container_match = re.search(
            r'(?:restart|stop|kill)\s+(?:container\s+)?(\S+)',
            action_lower,
        )
        if container_match:
            container = container_match.group(1)
            checks["container_detail"] = _run_on_server(
                self._inventory, server,
                f"docker inspect --format "
                f"'{{{{.State.StartedAt}}}}|{{{{.RestartCount}}}}|"
                f"{{{{range .NetworkSettings.Ports}}}}{{{{.}}}}{{{{end}}}}' "
                f"{container} 2>/dev/null",
            )
            checks["container_connections"] = _run_on_server(
                self._inventory, server,
                f"docker exec {container} ss -tn state established 2>/dev/null | wc -l || echo 0",
            )

        keys = list(checks.keys())
        results = await asyncio.gather(*[checks[k] for k in keys])
        data = dict(zip(keys, results))

        return ToolResult(output=_build_blast_report(server, action, data))


def _v(data: dict[str, ToolResult], key: str) -> str:
    r = data.get(key)
    return r.output.strip() if r and r.success else ""


def _build_blast_report(server: str, action: str, data: dict[str, ToolResult]) -> str:
    """Build a blast radius assessment."""
    lines: list[str] = [f"# Blast Radius Preview: {action}\n"]
    lines.append(f"**Server:** {server}")

    action_lower = action.lower()
    risk_level = "LOW"

    # ── Current State ──
    containers = _v(data, "containers")
    container_count = 0
    container_list: list[str] = []
    if containers:
        container_list = [l for l in containers.splitlines() if l.strip()]
        container_count = len(container_list)
        lines.append(f"**Running containers:** {container_count}")

    connections = _v(data, "connections")
    conn_count = 0
    if connections:
        try:
            conn_count = int(connections.strip()) - 1  # subtract header
            conn_count = max(0, conn_count)
            lines.append(f"**Active connections:** {conn_count}")
        except ValueError:
            pass

    # ── Port breakdown ──
    port_conns = _v(data, "port_connections")
    if port_conns:
        lines.append("\n**Connections by port:**")
        game_ports = set()
        web_connections = 0
        for line in port_conns.splitlines()[:10]:
            parts = line.strip().split()
            if len(parts) >= 2:
                count = int(parts[0])
                port = parts[1]
                lines.append(f"  :{port} — {count} connections")
                # Classify
                if port in ("80", "443", "8080", "8443"):
                    web_connections += count
                elif port not in ("22",):
                    game_ports.add(port)

        if web_connections > 0:
            lines.append(f"\n  ~{web_connections} web visitors currently connected")
        if game_ports:
            lines.append(f"  Game server ports active: {', '.join(sorted(game_ports))}")

    # ── Impact Assessment ──
    lines.append("\n## Impact Assessment\n")

    if "reboot" in action_lower:
        risk_level = "CRITICAL"
        lines.append(f"⚠ **REBOOT** will affect ALL {container_count} containers")
        lines.append(f"  and disconnect ALL {conn_count} active connections.")
        if container_list:
            lines.append("\n  **Affected containers:**")
            for c in container_list:
                name = c.split("|")[0]
                lines.append(f"    - {name}")

    elif "restart docker" in action_lower or "stop docker" in action_lower:
        risk_level = "CRITICAL"
        lines.append(f"✗ **Docker restart/stop** will take down ALL {container_count} containers")
        lines.append(f"  and disconnect {conn_count} active connections.")
        if container_list:
            lines.append("\n  **All containers will restart:**")
            for c in container_list:
                name = c.split("|")[0]
                lines.append(f"    - {name}")

    elif "restart" in action_lower or "stop" in action_lower:
        # Check known services first before treating as a container
        known_services = {
            ("mysql", "mariadb"): lambda: (
                "HIGH",
                "⚠ **MySQL restart** will briefly disconnect ALL database clients",
                "  All websites using this database will show errors during restart.",
            ),
            ("nginx", "apache", "httpd", "litespeed"): lambda: (
                "HIGH",
                f"⚠ **Web server restart** will briefly drop {conn_count} connections",
                "  Visitors may see brief errors during the restart window.",
            ),
            ("pterodactyl", "wings"): lambda: (
                "HIGH",
                f"⚠ **Wings restart** will temporarily disconnect ALL game servers",
                f"  {container_count} game server containers may restart.",
            ),
        }

        service_matched = False
        for svc_names, builder in known_services.items():
            if any(svc in action_lower for svc in svc_names):
                risk_level, msg1, msg2 = builder()
                lines.append(msg1)
                lines.append(msg2)
                service_matched = True
                break

        if not service_matched:
            # Treat as a container restart
            container_match = re.search(
                r'(?:restart|stop|kill)\s+(?:container\s+)?(\S+)',
                action_lower,
            )
            if container_match:
                target = container_match.group(1)
                container_conns = _v(data, "container_connections")
                try:
                    c_conns = int(container_conns.strip())
                except (ValueError, AttributeError):
                    c_conns = 0

                if c_conns > 0:
                    risk_level = "HIGH"
                    lines.append(f"⚠ Container **{target}** has **{c_conns} active connections**")
                    lines.append(f"  These connections will be dropped on restart.")
                else:
                    risk_level = "MEDIUM"
                    lines.append(f"Container **{target}** has no active connections.")

                detail = _v(data, "container_detail")
                if detail:
                    parts = detail.split("|")
                    if len(parts) >= 2:
                        started = parts[0]
                        restarts = parts[1]
                        lines.append(f"  Started: {started}")
                        if restarts != "0":
                            lines.append(f"  Previous restarts: {restarts}")
            else:
                risk_level = "MEDIUM"
                lines.append(f"Service restart may briefly affect dependent services.")

    # ── Risk Level ──
    risk_colors = {
        "CRITICAL": "✗✗✗",
        "HIGH": "✗✗",
        "MEDIUM": "⚠",
        "LOW": "✓",
    }
    lines.append(f"\n---\n**Risk Level: {risk_colors.get(risk_level, '?')} {risk_level}**")

    if risk_level in ("CRITICAL", "HIGH"):
        lines.append(
            "\nConsider: Is this the right time? Are there active players/visitors "
            "who should be warned? Is there a maintenance window?"
        )
    elif risk_level == "MEDIUM":
        lines.append("\nImpact should be minimal but monitor after the action.")

    return "\n".join(lines)
