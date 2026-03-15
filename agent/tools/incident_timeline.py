"""Incident timeline builder.

Automatically constructs a chronological timeline of events during
an incident by pulling timestamps from logs, dmesg, Docker events,
and systemd journals. Helps see the sequence of failures.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from agent.inventory import Inventory
from agent.tools.base import BaseTool, ToolResult
from agent.tools.docker_tools import _run_on_server


class IncidentTimeline(BaseTool):
    """Build a chronological incident timeline from logs and events."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "incident_timeline"

    @property
    def description(self) -> str:
        return (
            "Build a chronological timeline of an incident from logs, "
            "dmesg, Docker events, and systemd journals. Shows the "
            "sequence of failures to identify root cause and cascade."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server to investigate.",
                },
                "since": {
                    "type": "string",
                    "description": "How far back to look: '1h', '30m', '4h'. Default: '1h'.",
                    "default": "1h",
                },
                "keyword": {
                    "type": "string",
                    "description": "Focus on events matching this keyword (optional).",
                },
            },
            "required": ["server"],
        }

    async def execute(
        self, *, server: str, since: str = "1h", keyword: str | None = None, **kwargs: Any,
    ) -> ToolResult:
        """Build incident timeline."""
        checks: dict[str, Any] = {
            # System journal (errors/warnings only for timeline)
            "journal_errors": _run_on_server(
                self._inventory, server,
                f"journalctl --no-pager -p err --since '{since} ago' "
                f"--output=short-iso 2>/dev/null | tail -50",
            ),
            "journal_warnings": _run_on_server(
                self._inventory, server,
                f"journalctl --no-pager -p warning --since '{since} ago' "
                f"--output=short-iso 2>/dev/null | tail -30",
            ),
            # Kernel messages
            "dmesg": _run_on_server(
                self._inventory, server,
                "dmesg -T --level=err,crit,alert,emerg 2>/dev/null | tail -20",
            ),
            # Docker events
            "docker_events": _run_on_server(
                self._inventory, server,
                f"docker events --since {since} --until 0s "
                f"--format '{{{{.Time}}}} [docker] {{{{.Action}}}} {{{{.Actor.Attributes.name}}}}' "
                f"2>/dev/null | tail -30",
            ),
            # Service state changes
            "service_changes": _run_on_server(
                self._inventory, server,
                f"journalctl --no-pager --since '{since} ago' -t systemd "
                f"--output=short-iso 2>/dev/null | "
                f"grep -iE 'start|stop|fail|restart|exited|killed' | tail -20",
            ),
            # OOM kills specifically
            "oom_kills": _run_on_server(
                self._inventory, server,
                f"journalctl --no-pager --since '{since} ago' "
                f"--output=short-iso 2>/dev/null | "
                f"grep -i 'oom\\|out of memory\\|killed process' | tail -10",
            ),
            # Container restarts
            "container_restarts": _run_on_server(
                self._inventory, server,
                "docker ps -a --format '{{.Names}}|{{.Status}}' 2>/dev/null",
            ),
        }

        keys = list(checks.keys())
        results = await asyncio.gather(*[checks[k] for k in keys])
        data = dict(zip(keys, results))

        return ToolResult(output=_build_timeline(server, since, keyword, data))


def _v(data: dict[str, ToolResult], key: str) -> str:
    r = data.get(key)
    return r.output.strip() if r and r.success else ""


def _build_timeline(
    server: str, since: str, keyword: str | None, data: dict[str, ToolResult],
) -> str:
    """Build a chronological incident timeline."""
    lines: list[str] = [f"# Incident Timeline: {server} (last {since})\n"]

    # Collect all events with timestamps
    events: list[tuple[str, str, str]] = []  # (timestamp, source, message)

    # Journal errors
    journal_err = _v(data, "journal_errors")
    if journal_err:
        for line in journal_err.splitlines():
            ts = _extract_timestamp(line)
            if ts:
                events.append((ts, "system", line.strip()))

    # Journal warnings
    journal_warn = _v(data, "journal_warnings")
    if journal_warn:
        for line in journal_warn.splitlines():
            ts = _extract_timestamp(line)
            if ts:
                events.append((ts, "system", line.strip()))

    # Kernel messages
    dmesg = _v(data, "dmesg")
    if dmesg:
        for line in dmesg.splitlines():
            if line.strip():
                ts = _extract_dmesg_timestamp(line)
                events.append((ts or "?", "kernel", line.strip()))

    # Docker events
    docker = _v(data, "docker_events")
    if docker:
        for line in docker.splitlines():
            if line.strip():
                parts = line.strip().split(" ", 1)
                ts = parts[0] if parts else "?"
                msg = parts[1] if len(parts) > 1 else line
                events.append((ts, "docker", msg))

    # Service changes
    services = _v(data, "service_changes")
    if services:
        for line in services.splitlines():
            ts = _extract_timestamp(line)
            if ts:
                events.append((ts, "systemd", line.strip()))

    # OOM kills
    oom = _v(data, "oom_kills")
    if oom:
        for line in oom.splitlines():
            ts = _extract_timestamp(line)
            if ts:
                events.append((ts, "oom", f"✗ {line.strip()}"))

    # Filter by keyword if specified
    if keyword:
        keyword_lower = keyword.lower()
        events = [e for e in events if keyword_lower in e[2].lower()]

    if not events:
        lines.append("No significant events found in the time window.")
        if keyword:
            lines.append(f"(Filtered for: '{keyword}')")
        return "\n".join(lines)

    # Sort by timestamp
    events.sort(key=lambda x: x[0])

    # Deduplicate very similar events (same source + similar message)
    deduped: list[tuple[str, str, str]] = []
    seen_messages: set[str] = set()
    for ts, source, msg in events:
        # Create a simplified key for dedup
        simple = re.sub(r'\d+', 'N', msg[:50])
        key = f"{source}:{simple}"
        if key not in seen_messages:
            deduped.append((ts, source, msg))
            seen_messages.add(key)

    # Format timeline
    lines.append("```")
    prev_ts = ""
    for ts, source, msg in deduped[-50:]:  # Cap at 50 events
        # Show time gaps
        if prev_ts and ts != prev_ts:
            ts_short = ts[:19] if len(ts) > 19 else ts
        else:
            ts_short = ts[:19] if len(ts) > 19 else ts

        icon = _get_severity_icon(msg)
        source_tag = f"[{source}]"
        # Truncate long messages
        msg_short = msg[:120] + "..." if len(msg) > 120 else msg
        lines.append(f"{ts_short}  {source_tag:<10} {icon} {msg_short}")
        prev_ts = ts
    lines.append("```")

    # Analysis
    lines.append(f"\n**{len(deduped)} events** in timeline")

    # Detect patterns
    oom_count = sum(1 for _, s, _ in deduped if s == "oom")
    docker_count = sum(1 for _, s, m in deduped if s == "docker" and "die" in m.lower())
    restart_count = sum(1 for _, s, m in deduped if "restart" in m.lower())

    if oom_count > 0:
        lines.append(f"\n✗ **{oom_count} OOM kill(s)** — memory exhaustion triggered the incident")
    if docker_count > 0:
        lines.append(f"\n✗ **{docker_count} container death(s)** — containers crashed or were killed")
    if restart_count > 0:
        lines.append(f"\n⚠ **{restart_count} restart(s)** — services or containers were restarted")

    # Container status (current)
    containers = _v(data, "container_restarts")
    if containers:
        problem_containers = [
            l for l in containers.splitlines()
            if "restarting" in l.lower() or ("exited" in l.lower() and "ago" in l.lower())
        ]
        if problem_containers:
            lines.append("\n## Currently Affected Containers")
            for c in problem_containers:
                parts = c.split("|")
                lines.append(f"  ✗ {parts[0]}: {parts[1] if len(parts) > 1 else 'unknown'}")

    return "\n".join(lines)


def _extract_timestamp(line: str) -> str:
    """Extract ISO-ish timestamp from a log line."""
    # ISO format: 2024-01-15T14:23:45
    match = re.match(r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})', line)
    if match:
        return match.group(1)
    # Syslog format: Mar 14 12:34:56
    match = re.match(r'([A-Z][a-z]{2}\s+\d+\s+\d{2}:\d{2}:\d{2})', line)
    if match:
        return match.group(1)
    return ""


def _extract_dmesg_timestamp(line: str) -> str:
    """Extract timestamp from dmesg -T output."""
    # [Mon Mar 14 12:34:56 2024]
    match = re.search(r'\[([^\]]+)\]', line)
    return match.group(1) if match else ""


def _get_severity_icon(message: str) -> str:
    """Get severity icon based on message content."""
    msg_lower = message.lower()
    if any(kw in msg_lower for kw in ("fatal", "panic", "oom", "killed", "crash", "segfault")):
        return "✗"
    if any(kw in msg_lower for kw in ("error", "fail", "die", "exited")):
        return "✗"
    if any(kw in msg_lower for kw in ("warn", "timeout", "refused", "restart")):
        return "⚠"
    return " "
