"""Comprehensive health check tool.

Runs a full diagnostic sweep across one or all servers and returns
a concise summary with issues flagged. Designed to be the go-to
tool for "is everything OK?" questions.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agent.inventory import Inventory, ServerInfo
from agent.tools.base import BaseTool, ToolResult


# Thresholds for flagging issues
_DISK_WARN_PCT = 80
_MEM_WARN_PCT = 85


class HealthCheck(BaseTool):
    """Run a comprehensive health check across one or all servers."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "health_check"

    @property
    def description(self) -> str:
        return (
            "Run a comprehensive health check on one server or all servers. "
            "Returns a concise summary with issues flagged (disk, memory, load, "
            "containers, services). Use this as the first step when investigating "
            "problems or doing routine checks."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": (
                        "Server name, or 'all' to check every server. "
                        "Default: 'all'."
                    ),
                },
            },
            "required": [],
        }

    async def execute(self, *, server: str = "all", **kwargs: Any) -> ToolResult:
        """Run health checks and aggregate results."""
        if server == "all":
            servers = [
                self._inventory.get_server(n)
                for n in self._inventory.server_names
            ]
        else:
            try:
                servers = [self._inventory.get_server(server)]
            except KeyError as e:
                return ToolResult(error=str(e), exit_code=1)

        results = await asyncio.gather(
            *[self._check_server(s) for s in servers],
            return_exceptions=True,
        )

        sections: list[str] = []
        for srv, result in zip(servers, results):
            if isinstance(result, Exception):
                sections.append(f"## {srv.name}\n✗ Error: {result}")
            else:
                sections.append(f"## {srv.name}\n{result}")

        return ToolResult(output="\n\n".join(sections))

    async def _check_server(self, server_info: ServerInfo) -> str:
        """Run all health checks for a single server."""
        is_local = not server_info.definition.ssh

        commands = {
            "uptime": "uptime",
            "disk": "df -h",
            "memory": "free -m",
        }

        # Add docker check for servers with docker service
        if "docker" in server_info.definition.services:
            commands["containers"] = "docker ps -a --format table {{.Names}}\\t{{.Status}}\\t{{.State}}"

        # Add service checks
        for svc in server_info.definition.services:
            if svc != "docker":
                commands[f"svc:{svc}"] = f"systemctl is-active {svc}"

        if is_local:
            raw = await self._run_local(commands)
        else:
            raw = await self._run_remote(server_info, commands)

        return self._analyze(server_info, raw)

    async def _run_local(self, commands: dict[str, str]) -> dict[str, str]:
        """Run commands locally and return output map."""
        results: dict[str, str] = {}
        for label, cmd in commands.items():
            args = cmd.split()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
                results[label] = stdout.decode("utf-8", errors="replace").rstrip()
                if proc.returncode != 0 and stderr:
                    results[label] = f"ERROR:{stderr.decode('utf-8', errors='replace').rstrip()}"
            except asyncio.TimeoutError:
                results[label] = "ERROR:timed out"
            except Exception as e:
                results[label] = f"ERROR:{e}"
        return results

    async def _run_remote(
        self, server_info: ServerInfo, commands: dict[str, str]
    ) -> dict[str, str]:
        """Run commands on a remote server via SSH."""
        from agent.tools.remote import run_remote_command

        results: dict[str, str] = {}
        for label, cmd in commands.items():
            result = await run_remote_command(server_info, cmd, timeout=15)
            if result.success:
                results[label] = result.output
            else:
                results[label] = f"ERROR:{result.error}"
        return results

    def _analyze(self, server_info: ServerInfo, raw: dict[str, str]) -> str:
        """Analyze raw command outputs and produce a summary with flags."""
        lines: list[str] = []
        issues: list[str] = []

        # Uptime / load
        uptime_raw = raw.get("uptime", "")
        if uptime_raw and not uptime_raw.startswith("ERROR:"):
            lines.append(f"Uptime: {uptime_raw.strip()}")
            # Check load average
            if "load average:" in uptime_raw:
                try:
                    load_str = uptime_raw.split("load average:")[1].strip()
                    load_1m = float(load_str.split(",")[0].strip())
                    if load_1m > 4.0:
                        issues.append(f"⚠ High load: {load_1m:.1f}")
                except (IndexError, ValueError):
                    pass
        elif uptime_raw.startswith("ERROR:"):
            issues.append(f"✗ Uptime check failed: {uptime_raw[6:]}")

        # Disk usage
        disk_raw = raw.get("disk", "")
        if disk_raw and not disk_raw.startswith("ERROR:"):
            disk_problems = _parse_disk(disk_raw)
            if disk_problems:
                issues.extend(disk_problems)
            else:
                lines.append("Disk: OK")
        elif disk_raw.startswith("ERROR:"):
            issues.append(f"✗ Disk check failed: {disk_raw[6:]}")

        # Memory
        mem_raw = raw.get("memory", "")
        if mem_raw and not mem_raw.startswith("ERROR:"):
            mem_issue = _parse_memory(mem_raw)
            if mem_issue:
                issues.append(mem_issue)
            else:
                lines.append("Memory: OK")
        elif mem_raw.startswith("ERROR:"):
            issues.append(f"✗ Memory check failed: {mem_raw[6:]}")

        # Containers
        containers_raw = raw.get("containers", "")
        if containers_raw and not containers_raw.startswith("ERROR:"):
            container_issues = _parse_containers(containers_raw)
            if container_issues:
                issues.extend(container_issues)
            else:
                lines.append("Containers: all healthy")
        elif containers_raw.startswith("ERROR:"):
            issues.append(f"✗ Docker check failed: {containers_raw[6:]}")

        # Services
        for key, val in raw.items():
            if not key.startswith("svc:"):
                continue
            svc_name = key[4:]
            if val.startswith("ERROR:"):
                issues.append(f"✗ {svc_name}: check failed ({val[6:]})")
            elif val.strip() != "active":
                issues.append(f"✗ {svc_name}: {val.strip()}")

        # Build summary
        if issues:
            lines.insert(0, f"**Issues ({len(issues)}):**")
            for issue in issues:
                lines.insert(1 + issues.index(issue), f"  {issue}")
        else:
            lines.insert(0, "✓ All clear")

        return "\n".join(lines)


def _parse_disk(df_output: str) -> list[str]:
    """Parse df -h output and return warnings for high usage."""
    issues: list[str] = []
    for line in df_output.splitlines()[1:]:  # Skip header
        parts = line.split()
        if len(parts) < 5:
            continue
        use_str = parts[4].rstrip("%")
        try:
            use_pct = int(use_str)
        except ValueError:
            continue
        mount = parts[5] if len(parts) > 5 else parts[4]
        # Skip tiny/virtual filesystems
        if mount in ("/boot/efi", "/dev", "/dev/shm", "/run", "/sys"):
            continue
        if mount.startswith("/snap/"):
            continue
        if use_pct >= _DISK_WARN_PCT:
            size = parts[1]
            avail = parts[3]
            issues.append(f"⚠ Disk {mount}: {use_pct}% used ({avail} free of {size})")
    return issues


def _parse_memory(free_output: str) -> str | None:
    """Parse free -m output and return warning if memory is high."""
    for line in free_output.splitlines():
        if not line.startswith("Mem:"):
            continue
        parts = line.split()
        if len(parts) < 3:
            return None
        try:
            total = int(parts[1])
            used = int(parts[2])
        except ValueError:
            return None
        if total == 0:
            return None
        pct = (used / total) * 100
        if pct >= _MEM_WARN_PCT:
            return f"⚠ Memory: {pct:.0f}% used ({used}M / {total}M)"
    return None


def _parse_containers(docker_output: str) -> list[str]:
    """Parse docker ps output and flag unhealthy/restarting/exited containers."""
    issues: list[str] = []
    for line in docker_output.splitlines():
        lower = line.lower()
        if any(state in lower for state in ("exited", "restarting", "dead", "unhealthy")):
            # Extract container name (first column)
            name = line.split()[0] if line.split() else line.strip()
            # Extract status
            if "exited" in lower:
                issues.append(f"✗ Container {name}: exited")
            elif "restarting" in lower:
                issues.append(f"⚠ Container {name}: restarting (crash loop?)")
            elif "dead" in lower:
                issues.append(f"✗ Container {name}: dead")
            elif "unhealthy" in lower:
                issues.append(f"⚠ Container {name}: unhealthy")
    return issues
