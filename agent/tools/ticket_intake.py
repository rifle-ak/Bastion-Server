"""Customer ticket intake — paste a complaint, get a diagnosis.

The #1 time sink for hosting operators: translating vague customer
messages into technical investigation steps. This tool takes raw
customer text, classifies the issue type, and tells Claude exactly
which diagnostic tools to run.

"My server is laggy" → game_server_diagnose
"My website shows an error" → diagnose_site + page_debug
"I can't connect" → network/port/container checks
"My email isn't working" → email diagnostics

This is a routing tool — it analyzes the complaint and returns a
structured diagnostic plan that Claude then executes.
"""

from __future__ import annotations

import re
from typing import Any

from agent.tools.base import BaseTool, ToolResult


class TicketIntake(BaseTool):
    """Analyze a customer complaint and generate a diagnostic plan."""

    @property
    def name(self) -> str:
        return "ticket_intake"

    @property
    def description(self) -> str:
        return (
            "Paste a raw customer complaint or support ticket. Classifies "
            "the issue type (lag, crash, website error, email, connectivity, "
            "billing/resource) and generates a diagnostic plan telling you "
            "exactly which tools to run. Saves time translating vague "
            "complaints into technical investigation."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "ticket_text": {
                    "type": "string",
                    "description": "The raw customer complaint or ticket text.",
                },
                "server": {
                    "type": "string",
                    "description": "Server name if known (optional).",
                },
                "service_type": {
                    "type": "string",
                    "description": "Service type if known: 'game_server', 'website', 'email'. Auto-detected if omitted.",
                },
            },
            "required": ["ticket_text"],
        }

    async def execute(
        self, *, ticket_text: str, server: str = "", service_type: str = "", **kwargs: Any,
    ) -> ToolResult:
        """Analyze ticket and generate diagnostic plan."""
        classification = _classify_ticket(ticket_text, service_type)
        plan = _build_diagnostic_plan(classification, ticket_text, server)
        return ToolResult(output=plan)


def _classify_ticket(text: str, hint: str = "") -> dict[str, Any]:
    """Classify a customer complaint into issue categories."""
    text_lower = text.lower()
    result: dict[str, Any] = {
        "categories": [],
        "severity": "normal",
        "service_type": hint or "unknown",
        "keywords": [],
    }

    # ── Service type detection ──
    if not hint:
        game_signals = [
            "server", "lag", "rubber", "tps", "tick", "minecraft", "rust",
            "ark", "valheim", "cs2", "csgo", "terraria", "player", "slot",
            "mod", "plugin", "join", "connect", "game", "world",
            "pterodactyl", "panel",
        ]
        web_signals = [
            "website", "site", "page", "wordpress", "wp", "html", "css",
            "php", "error 5", "500", "503", "404", "ssl", "https",
            "domain", "cpanel", "loading", "blank", "white screen",
        ]
        email_signals = [
            "email", "mail", "smtp", "inbox", "spam", "bounce",
            "delivery", "sending", "receiving", "outlook", "gmail",
        ]

        game_score = sum(1 for s in game_signals if s in text_lower)
        web_score = sum(1 for s in web_signals if s in text_lower)
        email_score = sum(1 for s in email_signals if s in text_lower)

        if game_score > web_score and game_score > email_score:
            result["service_type"] = "game_server"
        elif web_score > game_score and web_score > email_score:
            result["service_type"] = "website"
        elif email_score > 0:
            result["service_type"] = "email"

    # ── Issue category detection ──
    # Performance / Lag
    lag_patterns = [
        r"lag", r"slow", r"rubber.?band", r"stutter", r"freez",
        r"tps", r"tick", r"fps drop", r"delay", r"latency",
        r"takes? (?:too )?long", r"loading",
    ]
    if any(re.search(p, text_lower) for p in lag_patterns):
        result["categories"].append("performance")
        result["keywords"].extend(["lag", "performance"])

    # Crash / Down
    crash_patterns = [
        r"crash", r"down", r"not (?:work|start|run|load|respond)",
        r"offline", r"can'?t (?:access|connect|reach|open|load)",
        r"unreach", r"timed? ?out", r"refused", r"dead",
        r"keeps? (?:crash|restart|stop|die|going down)",
    ]
    if any(re.search(p, text_lower) for p in crash_patterns):
        result["categories"].append("crash")
        result["keywords"].extend(["crash", "down"])

    # Error messages
    error_patterns = [
        r"error", r"500", r"503", r"502", r"404",
        r"white (?:screen|page)", r"blank (?:page|screen)",
        r"broken", r"display", r"show(?:s|ing)", r"visible",
        r"bleeding", r"code block",
    ]
    if any(re.search(p, text_lower) for p in error_patterns):
        result["categories"].append("error")
        result["keywords"].extend(["error", "display"])

    # Connectivity
    conn_patterns = [
        r"can'?t (?:connect|join|log ?in|access)",
        r"disconnect", r"kick", r"time ?out",
        r"connection (?:refused|lost|reset|closed)",
        r"port", r"firewall",
    ]
    if any(re.search(p, text_lower) for p in conn_patterns):
        result["categories"].append("connectivity")
        result["keywords"].extend(["connection", "network"])

    # Email specific
    email_patterns = [
        r"not (?:receiv|deliver|send|get)",
        r"bounce", r"spam", r"queue", r"reject",
        r"dkim", r"spf", r"dmarc",
    ]
    if any(re.search(p, text_lower) for p in email_patterns):
        result["categories"].append("email")
        result["keywords"].extend(["email", "delivery"])

    # Security
    security_patterns = [
        r"hack", r"malware", r"virus", r"compromise",
        r"deface", r"inject", r"phish", r"spam(?:ming)?",
        r"unauthori[sz]ed", r"suspicious",
    ]
    if any(re.search(p, text_lower) for p in security_patterns):
        result["categories"].append("security")
        result["keywords"].extend(["security", "compromise"])
        result["severity"] = "urgent"

    # Resource / Billing
    resource_patterns = [
        r"disk (?:full|space)", r"storage", r"memory",
        r"ram", r"cpu", r"limit", r"quota", r"upgrade",
        r"more (?:ram|memory|cpu|storage|space)",
    ]
    if any(re.search(p, text_lower) for p in resource_patterns):
        result["categories"].append("resources")
        result["keywords"].extend(["resources", "capacity"])

    # Severity escalation
    urgency_patterns = [
        r"urgent", r"asap", r"emergency", r"critical",
        r"produc(?:tion|e)", r"revenue", r"losing (?:money|customer)",
        r"all (?:player|user|client|customer)", r"everyone",
    ]
    if any(re.search(p, text_lower) for p in urgency_patterns):
        result["severity"] = "urgent"

    # Default if nothing matched
    if not result["categories"]:
        result["categories"].append("general")

    return result


def _build_diagnostic_plan(
    classification: dict[str, Any], ticket_text: str, server: str,
) -> str:
    """Build a diagnostic plan based on the classification."""
    lines: list[str] = ["# Ticket Analysis\n"]

    svc = classification["service_type"]
    cats = classification["categories"]
    severity = classification["severity"]

    # Header
    svc_label = {
        "game_server": "Game Server",
        "website": "Website/Web Hosting",
        "email": "Email",
        "unknown": "General Infrastructure",
    }.get(svc, svc.title())

    lines.append(f"**Service type:** {svc_label}")
    lines.append(f"**Issue categories:** {', '.join(cats)}")
    if severity == "urgent":
        lines.append(f"**Severity:** ✗ URGENT — customer indicates high impact")
    lines.append(f"**Server:** {server or 'Not specified — identify from context'}")

    # Customer complaint summary
    lines.append(f"\n**Customer says:**\n> {ticket_text[:500]}")

    # ── Diagnostic Plan ──
    lines.append("\n## Diagnostic Plan\n")
    lines.append("Run these tools in order:\n")

    step = 1

    if svc == "game_server":
        if "performance" in cats or "crash" in cats:
            lines.append(f"**{step}.** `game_server_diagnose` — Deep lag/crash analysis")
            lines.append(f"   Check CPU throttling, memory pressure, I/O, GC pauses, noisy neighbors")
            step += 1

        if "connectivity" in cats:
            lines.append(f"**{step}.** `run_remote_command` — Check container status and ports")
            lines.append(f"   Commands: `docker ps`, `ss -tlnp`, `docker logs --tail 50 <container>`")
            step += 1

        if "crash" in cats:
            lines.append(f"**{step}.** `incident_timeline` — Build event timeline")
            lines.append(f"   See what happened leading up to the crash")
            step += 1
            lines.append(f"**{step}.** `what_changed` — Check for recent changes")
            lines.append(f"   Did a mod update, config change, or image pull cause this?")
            step += 1

        lines.append(f"**{step}.** `explain_to_client` — Generate client response (service_type='game_server')")
        step += 1

    elif svc == "website":
        if "error" in cats:
            lines.append(f"**{step}.** `diagnose_site` — One-shot site diagnosis")
            lines.append(f"   DNS, SSL, Apache, PHP, WordPress checks")
            step += 1
            lines.append(f"**{step}.** `page_debug` — Check for PHP errors, broken HTML, code bleeding")
            step += 1

        if "performance" in cats:
            lines.append(f"**{step}.** `wp_deep_performance` — WordPress performance deep dive")
            lines.append(f"   TTFB, autoload bloat, OPcache, object cache, DB bloat")
            step += 1

        if "crash" in cats or "connectivity" in cats:
            lines.append(f"**{step}.** `service_status` — Check Apache/Nginx/PHP-FPM")
            step += 1
            lines.append(f"**{step}.** `web_error_log` — Recent error log entries")
            step += 1

        if "security" in cats:
            lines.append(f"**{step}.** `wp_security_scan` — Malware and integrity check")
            step += 1
            lines.append(f"**{step}.** `security_audit` — Server hardening check")
            step += 1

        lines.append(f"**{step}.** `explain_to_client` — Generate client response (service_type='website')")
        step += 1

    elif svc == "email":
        lines.append(f"**{step}.** `cpanel_email_diag` — Email deliverability diagnostics")
        step += 1
        lines.append(f"**{step}.** `cpanel_mail_queue` — Check mail queue for stuck messages")
        step += 1
        lines.append(f"**{step}.** `dns_check` — Verify MX, SPF, DKIM, DMARC records")
        step += 1
        lines.append(f"**{step}.** `explain_to_client` — Generate client response (service_type='email')")
        step += 1

    else:
        # General / unknown
        lines.append(f"**{step}.** `infrastructure_pulse` — Quick health check across all servers")
        step += 1
        if server:
            lines.append(f"**{step}.** `get_server_status` — Detailed status of {server}")
            step += 1
        if "performance" in cats:
            lines.append(f"**{step}.** `get_server_status` — Check CPU, memory, disk, load")
            step += 1
        if "crash" in cats:
            lines.append(f"**{step}.** `incident_timeline` — Build event timeline")
            step += 1
            lines.append(f"**{step}.** `what_changed` — Recent changes")
            step += 1
        lines.append(f"**{step}.** `explain_to_client` — Generate client response")
        step += 1

    # Blast radius reminder
    lines.append(
        "\n**Before any fix:** Run `blast_radius` to check impact."
    )
    lines.append(
        "**After fixing:** Run `explain_to_client` to generate the ticket response."
    )

    return "\n".join(lines)
