"""Comprehensive health check tool and direct-mode runner.

Runs a full diagnostic sweep across servers and returns a concise
summary with issues flagged.  Can be called as a tool (through
Claude) or directly via ``run_health_check()`` for zero-API-cost
monitoring from cron jobs.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from agent.inventory import Inventory, ServerInfo
from agent.tools.base import BaseTool, ToolResult


# Thresholds for flagging issues
_DISK_WARN_PCT = 80
_MEM_WARN_PCT = 85
_LOAD_PER_CPU = 1.5  # flag if 1m load > this * nproc


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
            "Comprehensive health check on one or all servers. "
            "Checks disk, memory, load, containers, services, "
            "OOM kills, and I/O wait. Use as first step for "
            "investigations or routine checks."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server name or 'all' (default: 'all').",
                },
            },
            "required": [],
        }

    async def execute(self, *, server: str = "all", **kwargs: Any) -> ToolResult:
        """Run health checks and aggregate results."""
        return await run_health_check(self._inventory, server)


async def run_health_check(inventory: Inventory, server: str = "all") -> ToolResult:
    """Run health checks without going through the tool registry.

    This is the direct-mode entry point used by ``bastion monitor``
    to avoid Claude API costs.  Also called by the HealthCheck tool.

    Args:
        inventory: Server inventory.
        server: Server name or 'all'.

    Returns:
        ToolResult with the health report.
    """
    if server == "all":
        servers = [inventory.get_server(n) for n in inventory.server_names]
    else:
        try:
            servers = [inventory.get_server(server)]
        except KeyError as e:
            return ToolResult(error=str(e), exit_code=1)

    # Check all servers in parallel
    results = await asyncio.gather(
        *[_check_server(s) for s in servers],
        return_exceptions=True,
    )

    sections: list[str] = []
    any_issues = False
    for srv, result in zip(servers, results):
        if isinstance(result, Exception):
            sections.append(f"## {srv.name}\n  ✗ Error: {result}")
            any_issues = True
        else:
            report, has_issues = result
            sections.append(f"## {srv.name}\n{report}")
            if has_issues:
                any_issues = True

    return ToolResult(
        output="\n\n".join(sections),
        exit_code=1 if any_issues else 0,
    )


async def _check_server(server_info: ServerInfo) -> tuple[str, bool]:
    """Run all health checks for a single server.

    Returns:
        Tuple of (report_text, has_issues).
    """
    is_local = not server_info.definition.ssh

    commands: dict[str, str] = {
        "uptime": "uptime",
        "disk": "df -h",
        "memory": "free -m",
        "nproc": "nproc",
    }

    # Docker checks for servers with docker
    if "docker" in server_info.definition.services:
        commands["containers"] = (
            "docker ps -a --format "
            "table {{.Names}}\\t{{.Status}}\\t{{.State}}"
        )

    # Kernel messages — OOM kills and hardware errors
    commands["dmesg"] = "dmesg -T --level=err,crit,alert,emerg --nopager"

    # I/O wait from top (single snapshot)
    commands["cpu"] = "top -bn1 -1"

    # Network connection counts
    commands["connections"] = "ss -s"

    # Service checks
    for svc in server_info.definition.services:
        if svc != "docker":
            commands[f"svc:{svc}"] = f"systemctl is-active {svc}"

    if is_local:
        raw = await _run_local_parallel(commands)
    else:
        raw = await _run_remote_parallel(server_info, commands)

    return _analyze(server_info, raw)


async def _run_local_parallel(commands: dict[str, str]) -> dict[str, str]:
    """Run commands locally in parallel."""

    async def _run_one(label: str, cmd: str) -> tuple[str, str]:
        args = cmd.split()
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=10,
            )
            out = stdout.decode("utf-8", errors="replace").rstrip()
            if proc.returncode != 0 and not out and stderr:
                return label, f"ERROR:{stderr.decode('utf-8', errors='replace').rstrip()}"
            return label, out
        except asyncio.TimeoutError:
            return label, "ERROR:timed out"
        except Exception as e:
            return label, f"ERROR:{e}"

    pairs = await asyncio.gather(
        *[_run_one(lbl, cmd) for lbl, cmd in commands.items()]
    )
    return dict(pairs)


async def _run_remote_parallel(
    server_info: ServerInfo, commands: dict[str, str],
) -> dict[str, str]:
    """Run commands on a remote server, reusing one SSH connection."""
    try:
        import asyncssh
    except ImportError:
        return {k: "ERROR:asyncssh not available" for k in commands}

    defn = server_info.definition
    if not defn.key_path:
        return {k: f"ERROR:no SSH key for {server_info.name}" for k in commands}

    try:
        conn = await asyncio.wait_for(
            asyncssh.connect(
                defn.host,
                username=defn.user,
                client_keys=[defn.key_path],
                known_hosts=defn.known_hosts_path,
            ),
            timeout=10,
        )
    except asyncio.TimeoutError:
        return {k: f"ERROR:SSH connect timeout ({defn.host})" for k in commands}
    except Exception as e:
        return {k: f"ERROR:SSH failed: {e}" for k in commands}

    async def _run_one(label: str, cmd: str) -> tuple[str, str]:
        try:
            result = await asyncio.wait_for(conn.run(cmd, check=False), timeout=15)
            out = (result.stdout or "").rstrip()
            err = (result.stderr or "").rstrip()
            if result.exit_status != 0 and not out and err:
                return label, f"ERROR:{err}"
            return label, out
        except asyncio.TimeoutError:
            return label, "ERROR:timed out"
        except Exception as e:
            return label, f"ERROR:{e}"

    try:
        async with conn:
            pairs = await asyncio.gather(
                *[_run_one(lbl, cmd) for lbl, cmd in commands.items()]
            )
    except Exception as e:
        return {k: f"ERROR:{e}" for k in commands}

    return dict(pairs)


def _analyze(
    server_info: ServerInfo, raw: dict[str, str],
) -> tuple[str, bool]:
    """Analyze raw outputs and produce summary with flags.

    Returns:
        Tuple of (report_text, has_issues).
    """
    lines: list[str] = []
    issues: list[str] = []

    # --- Uptime / load ---
    uptime_raw = raw.get("uptime", "")
    nproc_raw = raw.get("nproc", "")
    if uptime_raw and not uptime_raw.startswith("ERROR:"):
        # Extract just the essentials
        uptime_short = uptime_raw.strip()
        lines.append(f"Uptime: {uptime_short}")
        if "load average:" in uptime_raw:
            try:
                load_str = uptime_raw.split("load average:")[1].strip()
                load_1m = float(load_str.split(",")[0].strip())
                ncpu = int(nproc_raw.strip()) if nproc_raw and not nproc_raw.startswith("ERROR:") else 1
                threshold = ncpu * _LOAD_PER_CPU
                if load_1m > threshold:
                    issues.append(
                        f"⚠ High load: {load_1m:.1f} "
                        f"(threshold: {threshold:.0f} for {ncpu} CPUs)"
                    )
            except (IndexError, ValueError):
                pass
    elif uptime_raw.startswith("ERROR:"):
        issues.append(f"✗ Uptime: {uptime_raw[6:]}")

    # --- Disk ---
    disk_raw = raw.get("disk", "")
    if disk_raw and not disk_raw.startswith("ERROR:"):
        disk_problems = _parse_disk(disk_raw)
        if disk_problems:
            issues.extend(disk_problems)
        else:
            lines.append("Disk: OK")
    elif disk_raw.startswith("ERROR:"):
        issues.append(f"✗ Disk: {disk_raw[6:]}")

    # --- Memory ---
    mem_raw = raw.get("memory", "")
    if mem_raw and not mem_raw.startswith("ERROR:"):
        mem_issue = _parse_memory(mem_raw)
        if mem_issue:
            issues.append(mem_issue)
        else:
            lines.append("Memory: OK")
    elif mem_raw.startswith("ERROR:"):
        issues.append(f"✗ Memory: {mem_raw[6:]}")

    # --- CPU / iowait ---
    cpu_raw = raw.get("cpu", "")
    if cpu_raw and not cpu_raw.startswith("ERROR:"):
        iowait = _parse_iowait(cpu_raw)
        if iowait is not None and iowait > 10.0:
            issues.append(f"⚠ High I/O wait: {iowait:.1f}%")

    # --- OOM kills ---
    dmesg_raw = raw.get("dmesg", "")
    if dmesg_raw and not dmesg_raw.startswith("ERROR:"):
        oom_count = _count_oom(dmesg_raw)
        if oom_count > 0:
            issues.append(f"✗ OOM kills detected: {oom_count} in dmesg")

    # --- Network connections ---
    conn_raw = raw.get("connections", "")
    if conn_raw and not conn_raw.startswith("ERROR:"):
        tcp_count = _parse_tcp_connections(conn_raw)
        if tcp_count is not None and tcp_count > 500:
            issues.append(f"⚠ High TCP connections: {tcp_count}")

    # --- Containers ---
    containers_raw = raw.get("containers", "")
    if containers_raw and not containers_raw.startswith("ERROR:"):
        container_issues = _parse_containers(containers_raw)
        if container_issues:
            issues.extend(container_issues)
        else:
            lines.append("Containers: all healthy")
    elif containers_raw.startswith("ERROR:"):
        issues.append(f"✗ Docker: {containers_raw[6:]}")

    # --- Services ---
    for key, val in sorted(raw.items()):
        if not key.startswith("svc:"):
            continue
        svc_name = key[4:]
        if val.startswith("ERROR:"):
            issues.append(f"✗ {svc_name}: {val[6:]}")
        elif val.strip() != "active":
            issues.append(f"✗ {svc_name}: {val.strip()}")

    # --- Build final report ---
    has_issues = bool(issues)
    if issues:
        report_lines = [f"**{len(issues)} issue(s):**"]
        for issue in issues:
            report_lines.append(f"  {issue}")
        report_lines.append("")
        report_lines.extend(lines)
    else:
        report_lines = ["✓ All clear"]
        report_lines.extend(lines)

    return "\n".join(report_lines), has_issues


# ── Parsers ──────────────────────────────────────────────────────


def _parse_disk(df_output: str) -> list[str]:
    """Parse df -h output and return warnings for high usage."""
    issues: list[str] = []
    skip_mounts = frozenset({
        "/boot/efi", "/dev", "/dev/shm", "/run", "/sys",
        "/run/lock", "/run/user",
    })
    for line in df_output.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 5:
            continue
        use_str = parts[4].rstrip("%")
        try:
            use_pct = int(use_str)
        except ValueError:
            continue
        mount = parts[5] if len(parts) > 5 else parts[4]
        if mount in skip_mounts or mount.startswith(("/snap/", "/run/")):
            continue
        if use_pct >= _DISK_WARN_PCT:
            size, avail = parts[1], parts[3]
            issues.append(
                f"⚠ Disk {mount}: {use_pct}% used ({avail} free of {size})"
            )
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
            total, used = int(parts[1]), int(parts[2])
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
    bad_states = {
        "exited": "exited",
        "restarting": "restarting (crash loop?)",
        "dead": "dead",
        "unhealthy": "unhealthy",
    }
    for line in docker_output.splitlines():
        lower = line.lower()
        for state, desc in bad_states.items():
            if state in lower:
                name = line.split()[0] if line.split() else line.strip()
                icon = "⚠" if state in ("restarting", "unhealthy") else "✗"
                issues.append(f"{icon} Container {name}: {desc}")
                break
    return issues


def _parse_iowait(top_output: str) -> float | None:
    """Extract I/O wait percentage from top -bn1 output."""
    # Look for: %Cpu(s):  1.0 us,  0.5 sy,  0.0 ni, 97.5 id,  1.0 wa, ...
    for line in top_output.splitlines():
        if "%Cpu" in line or "%cpu" in line.lower():
            match = re.search(r'(\d+\.?\d*)\s*wa', line)
            if match:
                return float(match.group(1))
    return None


def _count_oom(dmesg_output: str) -> int:
    """Count OOM killer invocations in dmesg output."""
    return sum(
        1 for line in dmesg_output.splitlines()
        if "oom-kill" in line.lower() or "out of memory" in line.lower()
    )


def _parse_tcp_connections(ss_output: str) -> int | None:
    """Extract total established TCP connections from ss -s output."""
    # ss -s output: "TCP:   150 (estab 42, closed 5, orphaned 0, ..."
    for line in ss_output.splitlines():
        if line.startswith("TCP:"):
            match = re.search(r'estab\s+(\d+)', line)
            if match:
                return int(match.group(1))
    return None
