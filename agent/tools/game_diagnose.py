"""Deep game server lag and performance diagnostics.

Goes beyond basic health checks to find the actual root cause of
player-reported issues like lag, rubberbanding, disconnects, and
server crashes. Analyzes CPU throttling, I/O bottlenecks, GC pauses,
network quality, entity overload, and noisy neighbors.

Works with Pterodactyl/Docker game servers (Minecraft, Rust, ARK,
CS2, Valheim, etc.)
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from agent.inventory import Inventory
from agent.tools.base import BaseTool, ToolResult
from agent.tools.docker_tools import _run_on_server


class GameServerDiagnose(BaseTool):
    """Deep diagnosis for game server lag and performance issues."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "game_server_diagnose"

    @property
    def description(self) -> str:
        return (
            "Deep diagnosis for game server lag/rubberbanding. Checks CPU "
            "throttling, I/O bottlenecks, memory pressure, GC pauses, "
            "network retransmissions, entity overload, and noisy neighbors. "
            "Give it a container name or ID."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Game server host.",
                },
                "container": {
                    "type": "string",
                    "description": "Docker container name or ID of the game server.",
                },
            },
            "required": ["server", "container"],
        }

    async def execute(self, *, server: str, container: str, **kwargs: Any) -> ToolResult:
        """Run deep game server diagnostics."""
        # Phase 1: Gather all data in parallel
        checks: dict[str, Any] = {
            # Container resource usage (CPU, memory, I/O, network)
            "stats": _run_on_server(
                self._inventory, server,
                f"docker stats --no-stream --format "
                f"'{{{{.CPUPerc}}}}|{{{{.MemUsage}}}}|{{{{.MemPerc}}}}|"
                f"{{{{.NetIO}}}}|{{{{.BlockIO}}}}|{{{{.PIDs}}}}' {container}",
            ),
            # CPU throttling — the #1 cause of rubberbanding
            "throttling": _run_on_server(
                self._inventory, server,
                f"docker exec {container} cat /sys/fs/cgroup/cpu.stat 2>/dev/null",
            ),
            # Cgroup v1 fallback
            "throttling_v1": _run_on_server(
                self._inventory, server,
                f"docker exec {container} cat /sys/fs/cgroup/cpu/cpu.stat 2>/dev/null",
            ),
            # Memory limit vs usage (OOM risk)
            "mem_limit": _run_on_server(
                self._inventory, server,
                f"docker exec {container} cat /sys/fs/cgroup/memory.max 2>/dev/null",
            ),
            "mem_current": _run_on_server(
                self._inventory, server,
                f"docker exec {container} cat /sys/fs/cgroup/memory.current 2>/dev/null",
            ),
            # Swap usage (instant lag if swapping)
            "mem_swap": _run_on_server(
                self._inventory, server,
                f"docker exec {container} cat /sys/fs/cgroup/memory.swap.current 2>/dev/null",
            ),
            # I/O wait on the host
            "iowait": _run_on_server(
                self._inventory, server, "top -bn1 -1",
            ),
            # Disk latency
            "iostat": _run_on_server(
                self._inventory, server, "iostat -x 1 2",
            ),
            # Network retransmissions (packet loss from server side)
            "tcp_retrans": _run_on_server(
                self._inventory, server, "ss -ti",
            ),
            # Network interface errors/drops
            "net_errors": _run_on_server(
                self._inventory, server, "ip -s link",
            ),
            # Process list inside container (thread count, CPU per process)
            "processes": _run_on_server(
                self._inventory, server,
                f"docker exec {container} ps aux --sort=-%cpu",
            ),
            # Container logs (last 100 lines for error/warning/lag detection)
            "logs": _run_on_server(
                self._inventory, server,
                f"docker logs --tail 100 {container} 2>&1",
            ),
            # Host load average and CPU count
            "uptime": _run_on_server(self._inventory, server, "uptime"),
            "nproc": _run_on_server(self._inventory, server, "nproc"),
            # Other containers on the same host (noisy neighbors)
            "all_containers": _run_on_server(
                self._inventory, server,
                "docker stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemPerc}}'",
            ),
            # Container inspect for resource limits
            "inspect": _run_on_server(
                self._inventory, server,
                f"docker inspect --format "
                f"'{{{{.HostConfig.CpuQuota}}}}|{{{{.HostConfig.CpuPeriod}}}}|"
                f"{{{{.HostConfig.Memory}}}}|{{{{.HostConfig.MemorySwap}}}}|"
                f"{{{{.State.StartedAt}}}}|{{{{.RestartCount}}}}' {container}",
            ),
            # OOM kills in dmesg
            "dmesg_oom": _run_on_server(
                self._inventory, server,
                "dmesg -T --level=err,crit,alert,emerg --nopager",
            ),
        }

        keys = list(checks.keys())
        results = await asyncio.gather(*[checks[k] for k in keys])
        data = dict(zip(keys, results))

        # Phase 2: Analyze and build report
        return ToolResult(output=_build_game_report(container, data))


def _val(data: dict[str, ToolResult], key: str) -> str:
    """Get output from a check result, or empty string."""
    r = data.get(key)
    if r and r.success:
        return r.output.strip()
    return ""


def _build_game_report(container: str, data: dict[str, ToolResult]) -> str:
    """Analyze all collected data and produce a lag diagnosis report."""
    sections: list[str] = [f"# Game Server Diagnosis: {container}\n"]
    findings: list[str] = []

    # ── Container Stats ──
    stats = _val(data, "stats")
    if stats:
        parts = stats.split("|")
        if len(parts) >= 6:
            sections.append(f"**CPU:** {parts[0]}  **Memory:** {parts[1]} ({parts[2]})")
            sections.append(f"**Network I/O:** {parts[3]}  **Block I/O:** {parts[4]}")
            sections.append(f"**PIDs:** {parts[5]}")

    # ── CPU Throttling (the #1 lag cause) ──
    sections.append("\n## CPU Throttling")
    throttle_data = _val(data, "throttling") or _val(data, "throttling_v1")
    if throttle_data:
        throttled = _extract_throttle(throttle_data)
        if throttled is not None:
            if throttled > 0:
                findings.append(
                    f"✗ CPU THROTTLED: {throttled} throttled periods detected. "
                    f"The container is hitting its CPU limit — this directly causes "
                    f"tick lag and rubberbanding. Increase CPU allocation or reduce "
                    f"server load (fewer players/entities/plugins)."
                )
                sections.append(f"✗ **{throttled} throttled periods** — CPU limit is too low")
            else:
                sections.append("✓ No CPU throttling detected")
        else:
            sections.append("Could not parse throttle data")
    else:
        sections.append("Cgroup CPU stats not available")

    # ── Resource Limits ──
    inspect = _val(data, "inspect")
    if inspect:
        iparts = inspect.split("|")
        if len(iparts) >= 6:
            cpu_quota = iparts[0]
            cpu_period = iparts[1]
            mem_limit = iparts[2]
            restart_count = iparts[5]

            if cpu_quota and cpu_period and cpu_quota != "0":
                try:
                    cores = int(cpu_quota) / int(cpu_period)
                    sections.append(f"**CPU limit:** {cores:.1f} cores")
                except (ValueError, ZeroDivisionError):
                    pass

            if mem_limit and mem_limit != "0":
                try:
                    mem_mb = int(mem_limit) / (1024 * 1024)
                    sections.append(f"**Memory limit:** {mem_mb:.0f} MB")
                except ValueError:
                    pass

            if restart_count and restart_count != "0":
                findings.append(f"⚠ Container has restarted {restart_count} times")

    # ── Memory Pressure ──
    sections.append("\n## Memory")
    mem_current = _val(data, "mem_current")
    mem_limit = _val(data, "mem_limit")
    mem_swap = _val(data, "mem_swap")

    if mem_current and mem_limit and mem_limit != "max":
        try:
            current_mb = int(mem_current) / (1024 * 1024)
            limit_mb = int(mem_limit) / (1024 * 1024)
            pct = (current_mb / limit_mb) * 100 if limit_mb > 0 else 0
            sections.append(f"Using {current_mb:.0f} MB / {limit_mb:.0f} MB ({pct:.0f}%)")
            if pct > 90:
                findings.append(
                    f"✗ MEMORY CRITICAL: {pct:.0f}% used. Server is about to OOM. "
                    f"Increase memory limit or reduce world size/player count."
                )
            elif pct > 75:
                findings.append(f"⚠ Memory at {pct:.0f}% — approaching limit")
        except ValueError:
            pass

    if mem_swap:
        try:
            swap_mb = int(mem_swap) / (1024 * 1024)
            if swap_mb > 10:
                findings.append(
                    f"✗ SWAPPING: {swap_mb:.0f} MB in swap. Swap is extremely slow "
                    f"and causes constant lag spikes. Increase memory limit immediately."
                )
                sections.append(f"✗ **{swap_mb:.0f} MB in swap**")
        except ValueError:
            pass

    # ── I/O Bottleneck ──
    sections.append("\n## Disk I/O")
    iowait_raw = _val(data, "iowait")
    if iowait_raw:
        match = re.search(r'(\d+\.?\d*)\s*wa', iowait_raw)
        if match:
            iowait = float(match.group(1))
            if iowait > 20:
                findings.append(
                    f"✗ HIGH I/O WAIT: {iowait:.1f}%. Disk is bottlenecking the server. "
                    f"This causes save lag and world loading stutters. "
                    f"Check if SSD, consider reducing autosave frequency."
                )
                sections.append(f"✗ I/O wait: {iowait:.1f}%")
            elif iowait > 5:
                findings.append(f"⚠ Elevated I/O wait: {iowait:.1f}%")
                sections.append(f"⚠ I/O wait: {iowait:.1f}%")
            else:
                sections.append(f"✓ I/O wait: {iowait:.1f}%")

    iostat = _val(data, "iostat")
    if iostat:
        await_match = re.findall(r'(\d+\.?\d+)\s+\d+\.?\d+\s*$', iostat, re.MULTILINE)
        high_latency = [float(a) for a in await_match if float(a) > 20]
        if high_latency:
            max_lat = max(high_latency)
            findings.append(f"⚠ Disk latency spikes up to {max_lat:.1f}ms (>20ms causes tick lag)")

    # ── Network Quality ──
    sections.append("\n## Network")
    tcp_raw = _val(data, "tcp_retrans")
    if tcp_raw:
        retrans_count = tcp_raw.count("retrans:")
        retrans_values = re.findall(r'retrans:\d+/(\d+)', tcp_raw)
        total_retrans = sum(int(v) for v in retrans_values) if retrans_values else 0
        if total_retrans > 100:
            findings.append(
                f"✗ HIGH RETRANSMISSIONS: {total_retrans} TCP retransmits. "
                f"Players are experiencing packet loss from the server side. "
                f"Check network route, MTU, or upstream provider issues."
            )
            sections.append(f"✗ {total_retrans} TCP retransmissions")
        elif total_retrans > 20:
            findings.append(f"⚠ {total_retrans} TCP retransmissions (some packet loss)")
            sections.append(f"⚠ {total_retrans} retransmissions")
        else:
            sections.append("✓ TCP retransmissions: normal")

    net_errors = _val(data, "net_errors")
    if net_errors:
        drops = re.findall(r'RX:.*?dropped\s+(\d+)', net_errors, re.DOTALL)
        total_drops = sum(int(d) for d in drops if int(d) > 0)
        if total_drops > 0:
            findings.append(f"⚠ Network interface dropping packets: {total_drops} drops")

    # ── GC / JVM Detection ──
    sections.append("\n## Application")
    logs = _val(data, "logs")
    if logs:
        gc_issues = _detect_gc_issues(logs)
        if gc_issues:
            findings.extend(gc_issues)
            for issue in gc_issues:
                sections.append(f"  {issue}")

        tick_issues = _detect_tick_issues(logs)
        if tick_issues:
            findings.extend(tick_issues)
            for issue in tick_issues:
                sections.append(f"  {issue}")

        crash_indicators = _detect_crashes(logs)
        if crash_indicators:
            findings.extend(crash_indicators)

        error_count = sum(
            1 for line in logs.splitlines()
            if any(kw in line.lower() for kw in ("error", "exception", "fatal", "panic"))
        )
        if error_count > 10:
            findings.append(f"⚠ {error_count} error/exception lines in recent logs")
        sections.append(f"Recent log errors: {error_count}")

    # ── Noisy Neighbors ──
    sections.append("\n## Noisy Neighbors")
    all_containers = _val(data, "all_containers")
    if all_containers:
        noisy = _find_noisy_neighbors(all_containers, container)
        if noisy:
            sections.append("Other containers using significant resources:")
            for name, cpu, mem in noisy:
                sections.append(f"  {name}: CPU {cpu}, Memory {mem}")
                if float(cpu.rstrip("%")) > 50:
                    findings.append(
                        f"⚠ Noisy neighbor: {name} using {cpu} CPU — "
                        f"could be stealing resources from {container}"
                    )
        else:
            sections.append("✓ No noisy neighbors")

    # ── OOM Kills ──
    dmesg = _val(data, "dmesg_oom")
    if dmesg:
        oom_lines = [l for l in dmesg.splitlines() if "oom" in l.lower() or "out of memory" in l.lower()]
        container_ooms = [l for l in oom_lines if container.lower() in l.lower()]
        if container_ooms:
            findings.append(
                f"✗ OOM KILL: This container has been killed by the OOM killer. "
                f"It ran out of memory. Increase memory limit."
            )
        elif oom_lines:
            findings.append(f"⚠ {len(oom_lines)} OOM kills on this host (may not be this container)")

    # ── Host Load ──
    uptime = _val(data, "uptime")
    nproc = _val(data, "nproc")
    if uptime and "load average:" in uptime:
        try:
            load_str = uptime.split("load average:")[1].strip()
            load_1m = float(load_str.split(",")[0].strip())
            ncpu = int(nproc) if nproc else 1
            if load_1m > ncpu * 2:
                findings.append(
                    f"✗ HOST OVERLOADED: Load {load_1m:.1f} on {ncpu} CPUs. "
                    f"The entire host is under heavy load — all containers suffer."
                )
        except (IndexError, ValueError):
            pass

    # ── Verdict ──
    sections.append("\n---")
    if findings:
        sections.append(f"\n## Findings ({len(findings)} issues)\n")
        # Sort: ✗ first, then ⚠
        critical = [f for f in findings if f.startswith("✗")]
        warnings = [f for f in findings if f.startswith("⚠")]
        for f in critical:
            sections.append(f)
        for f in warnings:
            sections.append(f)

        # Root cause suggestion
        sections.append("\n## Likely Root Cause")
        if any("THROTTLED" in f for f in critical):
            sections.append(
                "**CPU throttling** is the most likely cause of lag. The container "
                "is hitting its CPU limit and being paused by the kernel. Solutions:\n"
                "1. Increase CPU allocation in Pterodactyl Panel\n"
                "2. Reduce server load (fewer players, entities, or plugins)\n"
                "3. Move to a host with faster single-thread performance"
            )
        elif any("SWAP" in f for f in critical):
            sections.append(
                "**Memory swapping** is causing lag. The server is using disk as "
                "memory, which is 1000x slower. Increase memory allocation."
            )
        elif any("OOM" in f for f in critical):
            sections.append(
                "**Out of memory** — the server is being killed by the OOM killer. "
                "Increase memory limit or reduce world size."
            )
        elif any("I/O WAIT" in f for f in critical):
            sections.append(
                "**Disk I/O bottleneck** — world saves or chunk loading is blocking "
                "the game loop. Solutions:\n"
                "1. Use an SSD (NVMe preferred)\n"
                "2. Reduce autosave frequency\n"
                "3. Pre-generate the world to reduce chunk generation load"
            )
        elif any("RETRANSMISSION" in f.upper() for f in critical):
            sections.append(
                "**Network packet loss** from the server side. Players experience "
                "this as rubberbanding. Check:\n"
                "1. Server's network route (traceroute from player locations)\n"
                "2. MTU settings (try lowering to 1400)\n"
                "3. Contact upstream provider about packet loss"
            )
        elif any("OVERLOADED" in f for f in critical):
            sections.append(
                "**Host is overloaded** — too many containers for this hardware. "
                "Migrate some servers to another node."
            )
        else:
            sections.append(
                "No single critical root cause identified. The warnings above "
                "may combine to cause intermittent issues. Consider reviewing "
                "each one."
            )
    else:
        sections.append("\n## Verdict")
        sections.append(
            "✓ **No performance issues detected.** CPU, memory, I/O, and network "
            "all look healthy. If players are still reporting lag, the issue may be:\n"
            "- Client-side (player's internet/hardware)\n"
            "- Geographic latency (players far from server)\n"
            "- Game-specific issue (mod conflict, corrupt world data)"
        )

    return "\n".join(sections)


def _extract_throttle(cgroup_output: str) -> int | None:
    """Extract throttled_usec or nr_throttled from cgroup CPU stats."""
    # cgroup v2: throttled_usec
    match = re.search(r'throttled_usec\s+(\d+)', cgroup_output)
    if match:
        return int(match.group(1))
    # cgroup v1: nr_throttled
    match = re.search(r'nr_throttled\s+(\d+)', cgroup_output)
    if match:
        return int(match.group(1))
    return None


def _detect_gc_issues(logs: str) -> list[str]:
    """Detect garbage collection pauses in game server logs."""
    issues: list[str] = []

    # Java GC pauses (Minecraft, etc.)
    gc_pauses = re.findall(r'GC.*?(\d+\.?\d+)\s*ms', logs)
    long_pauses = [float(p) for p in gc_pauses if float(p) > 100]
    if long_pauses:
        max_pause = max(long_pauses)
        issues.append(
            f"✗ GC PAUSES: {len(long_pauses)} garbage collection pauses >100ms "
            f"(worst: {max_pause:.0f}ms). Each pause freezes the server. "
            f"Tune JVM flags: -XX:+UseG1GC -XX:MaxGCPauseMillis=50"
        )

    # .NET/Mono GC (Rust/Unity servers)
    if "gc" in logs.lower() and "pause" in logs.lower():
        mono_pauses = re.findall(r'GC.*?pause.*?(\d+)\s*ms', logs, re.IGNORECASE)
        long_mono = [int(p) for p in mono_pauses if int(p) > 50]
        if long_mono:
            issues.append(
                f"⚠ Mono/Unity GC pauses detected: {len(long_mono)} pauses >50ms"
            )

    return issues


def _detect_tick_issues(logs: str) -> list[str]:
    """Detect tick rate / TPS issues from game logs."""
    issues: list[str] = []

    # Minecraft: "Can't keep up! Is the server overloaded?"
    cant_keep_up = logs.count("Can't keep up")
    if cant_keep_up > 5:
        issues.append(
            f"✗ TICK LAG: '{cant_keep_up}x \"Can\\'t keep up!\" in recent logs. "
            f"Server is running behind — tick rate is below 20 TPS."
        )
    elif cant_keep_up > 0:
        issues.append(f"⚠ {cant_keep_up}x \"Can't keep up!\" warnings")

    # Rust: "Calling OnServerShutdown" / oxide timing
    if "took too long" in logs.lower():
        slow_count = sum(1 for l in logs.splitlines() if "took too long" in l.lower())
        issues.append(f"⚠ {slow_count} 'took too long' warnings — plugins may be causing lag")

    # Generic: "timeout", "lag", "overloaded"
    timeout_lines = sum(
        1 for l in logs.splitlines()
        if any(kw in l.lower() for kw in ("timeout", "timed out", "overloaded"))
    )
    if timeout_lines > 5:
        issues.append(f"⚠ {timeout_lines} timeout/overloaded messages in recent logs")

    return issues


def _detect_crashes(logs: str) -> list[str]:
    """Detect crash indicators in game server logs."""
    issues: list[str] = []

    crash_keywords = [
        "segmentation fault", "segfault", "sigsegv",
        "fatal error", "server crashed", "core dumped",
        "out of memory", "java.lang.OutOfMemoryError",
        "stack overflow", "abort",
    ]

    for keyword in crash_keywords:
        if keyword.lower() in logs.lower():
            issues.append(f"✗ CRASH INDICATOR: '{keyword}' found in recent logs")
            break  # One is enough

    return issues


def _find_noisy_neighbors(
    all_containers: str, target: str,
) -> list[tuple[str, str, str]]:
    """Find other containers using significant resources."""
    noisy: list[tuple[str, str, str]] = []
    for line in all_containers.splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        name, cpu, mem = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if name == target:
            continue
        try:
            cpu_val = float(cpu.rstrip("%"))
            if cpu_val > 10:
                noisy.append((name, cpu, mem))
        except ValueError:
            continue
    return sorted(noisy, key=lambda x: float(x[1].rstrip("%")), reverse=True)[:5]
