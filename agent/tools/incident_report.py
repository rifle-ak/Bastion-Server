"""Post-incident report generator.

Generates a structured post-incident report template that Claude fills
in from conversation context. This is a prompt-based tool — it builds
a detailed prompt with the report skeleton and platform-specific
guidance, then Claude produces the actual report content.
"""

from __future__ import annotations

from typing import Any

from agent.tools.base import BaseTool, ToolResult

_SEVERITY_LEVELS = ("critical", "major", "minor")

_SERVICE_TYPES = ("game_server", "website", "email", "infrastructure")

_SERVICE_GUIDANCE: dict[str, str] = {
    "game_server": (
        "- Include estimated player impact (active players affected, sessions interrupted)\n"
        "- Note server downtime in terms players understand (e.g. 'server was unreachable')\n"
        "- Address rollback considerations: was any world/player data lost or rolled back?\n"
        "- Mention Pterodactyl panel status if relevant\n"
        "- Consider wipe schedule impact if near a scheduled wipe"
    ),
    "website": (
        "- Assess SEO impact: did search engine crawlers encounter errors during downtime?\n"
        "- Note SSL certificate status and renewal timeline if relevant\n"
        "- Check if CDN/cache served stale content during the outage\n"
        "- Consider WordPress/cPanel-specific recovery steps taken\n"
        "- Note any .htaccess or DNS propagation factors"
    ),
    "email": (
        "- Report mail queue status: were messages queued, bounced, or lost?\n"
        "- Include bounced message count if available\n"
        "- Assess sender reputation impact (SPF/DKIM/DMARC failures during incident)\n"
        "- Note whether affected domains were temporarily blacklisted\n"
        "- Detail steps taken to flush queues and redeliver"
    ),
    "infrastructure": (
        "- Perform cascade failure analysis: which upstream failure caused downstream impact?\n"
        "- Identify redundancy gaps exposed by this incident\n"
        "- Note network-level impact (routing, DNS, firewall changes)\n"
        "- Assess monitoring coverage: did alerts fire promptly?\n"
        "- Consider single points of failure revealed"
    ),
}


def _build_report_prompt(params: dict[str, Any]) -> str:
    """Build a structured prompt for incident report generation.

    Takes a dict of all incident parameters and returns the formatted
    prompt string that instructs Claude to produce the full report.

    Args:
        params: Dictionary containing incident report parameters.
            Required keys: incident_summary, severity.
            Optional keys: service_type, affected_servers, root_cause,
            resolution, start_time, end_time, customer_facing.

    Returns:
        Formatted prompt string for Claude to fill in.
    """
    summary: str = params["incident_summary"]
    severity: str = params["severity"]
    service_type: str = params.get("service_type", "")
    affected_servers: str = params.get("affected_servers", "")
    root_cause: str = params.get("root_cause", "")
    resolution: str = params.get("resolution", "")
    start_time: str = params.get("start_time", "")
    end_time: str = params.get("end_time", "")
    customer_facing: bool = params.get("customer_facing", True)

    # Build the known-facts block so Claude uses real data, not guesses
    known_facts: list[str] = []
    if affected_servers:
        known_facts.append(f"- **Affected Servers:** {affected_servers}")
    if root_cause:
        known_facts.append(f"- **Root Cause (identified):** {root_cause}")
    if resolution:
        known_facts.append(f"- **Resolution:** {resolution}")
    if start_time:
        known_facts.append(f"- **Start Time:** {start_time}")
    if end_time:
        known_facts.append(f"- **End Time:** {end_time}")

    known_section = "\n".join(known_facts) if known_facts else "_No additional details provided._"

    # Duration guidance
    if start_time and end_time:
        duration_line = f"{start_time} to {end_time}"
    elif start_time:
        duration_line = f"{start_time} to ongoing"
    else:
        duration_line = "[Determine from conversation context]"

    # Service type display
    service_display = service_type.replace("_", " ").title() if service_type else "[Determine from context]"

    # Platform-specific guidance
    platform_block = ""
    if service_type and service_type in _SERVICE_GUIDANCE:
        platform_block = (
            f"\n## Platform-Specific Guidance ({service_display})\n\n"
            f"Include the following platform-specific considerations:\n"
            f"{_SERVICE_GUIDANCE[service_type]}\n"
        )

    # Customer communication section
    customer_section = ""
    if customer_facing:
        customer_section = (
            "\n## Customer Communication\n"
            "[Draft a message to send to affected customers. Be professional "
            "and empathetic. Acknowledge the disruption, explain what happened "
            "in non-technical terms, describe what was done, and reassure them "
            "about preventive measures. Do NOT include internal server names, "
            "IP addresses, or technical commands.]\n"
        )

    prompt = (
        f"## Incident Report Request\n\n"
        f"Generate a professional post-incident report based on the conversation "
        f"context and the details below. Use ONLY real data from the conversation "
        f"— do not fabricate logs, metrics, timestamps, or customer counts.\n\n"
        f"### Known Details\n\n"
        f"- **Incident:** {summary}\n"
        f"- **Severity:** {severity.upper()}\n"
        f"{known_section}\n\n"
        f"### Report Format\n\n"
        f"Produce the report in this exact structure:\n\n"
        f"---\n\n"
        f"# Incident Report: {summary}\n\n"
        f"## Overview\n"
        f"- **Severity:** {severity.upper()}\n"
        f"- **Duration:** {duration_line}\n"
        f"- **Affected Services:** {service_display}\n"
        f"- **Affected Servers:** {affected_servers or '[List from context]'}\n"
        f"- **Impact:** [Describe customer impact based on conversation findings]\n\n"
        f"## Timeline\n"
        f"[Reconstruct the incident timeline from the conversation context. "
        f"What was discovered, in what order? Include timestamps where available. "
        f"Use a chronological list format.]\n\n"
        f"## Root Cause Analysis\n"
        f"[Technical root cause — be specific, reference logs and metrics "
        f"from the conversation. If root cause is unknown, state that clearly "
        f"and list the most likely candidates.]\n\n"
        f"## Resolution\n"
        f"[What was done to fix it, step by step. Reference specific commands "
        f"or actions taken during the conversation.]\n\n"
        f"## Impact Assessment\n"
        f"- Affected customers: [number or estimate from context]\n"
        f"- Downtime duration: [calculated from timeline]\n"
        f"- Data loss: [yes/no, with details if applicable]\n\n"
        f"## Preventive Measures\n"
        f"[Specific, actionable measures to prevent recurrence. "
        f"At least 3 concrete recommendations.]\n"
        f"1. [Measure 1]\n"
        f"2. [Measure 2]\n"
        f"3. [Measure 3]\n"
        f"{customer_section}\n"
        f"## Lessons Learned\n"
        f"[What the team learned from this incident. Focus on process "
        f"improvements, monitoring gaps, and response time observations.]\n\n"
        f"---\n"
        f"{platform_block}\n"
        f"**Important:** Only include facts supported by the conversation. "
        f"Mark any uncertain information with '[UNCONFIRMED]'. "
        f"Do not guess customer counts or downtime durations — use data from "
        f"the investigation or mark as '[TO BE DETERMINED]'."
    )

    return prompt


class IncidentReport(BaseTool):
    """Generate a structured post-incident report from conversation context."""

    @property
    def name(self) -> str:
        return "incident_report"

    @property
    def description(self) -> str:
        return (
            "Generate a professional post-incident report. Produces a structured "
            "report covering timeline, root cause analysis, resolution, impact "
            "assessment, preventive measures, and customer communication. "
            "Uses conversation context to fill in details from the investigation. "
            "Call this after diagnosing and resolving an incident."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "incident_summary": {
                    "type": "string",
                    "description": "Brief description of what happened.",
                },
                "severity": {
                    "type": "string",
                    "enum": list(_SEVERITY_LEVELS),
                    "description": "Incident severity: 'critical', 'major', or 'minor'.",
                },
                "service_type": {
                    "type": "string",
                    "enum": list(_SERVICE_TYPES),
                    "description": (
                        "Type of service affected: 'game_server', 'website', "
                        "'email', or 'infrastructure'."
                    ),
                },
                "affected_servers": {
                    "type": "string",
                    "description": "Comma-separated list of affected server names.",
                },
                "root_cause": {
                    "type": "string",
                    "description": "Root cause of the incident, if identified.",
                },
                "resolution": {
                    "type": "string",
                    "description": "What was done to resolve the incident.",
                },
                "start_time": {
                    "type": "string",
                    "description": "When the incident started (e.g. '2024-01-15 14:30 UTC').",
                },
                "end_time": {
                    "type": "string",
                    "description": "When the incident was resolved (e.g. '2024-01-15 15:45 UTC').",
                },
                "customer_facing": {
                    "type": "boolean",
                    "description": "Include a customer communication section. Defaults to true.",
                    "default": True,
                },
            },
            "required": ["incident_summary", "severity"],
        }

    async def execute(
        self,
        *,
        incident_summary: str,
        severity: str,
        service_type: str = "",
        affected_servers: str = "",
        root_cause: str = "",
        resolution: str = "",
        start_time: str = "",
        end_time: str = "",
        customer_facing: bool = True,
        **kwargs: Any,
    ) -> ToolResult:
        """Generate a post-incident report prompt.

        Validates inputs and builds a structured prompt that Claude
        uses to produce the full incident report from conversation context.
        """
        if severity not in _SEVERITY_LEVELS:
            return ToolResult(
                error=f"Invalid severity '{severity}'. Must be one of: {', '.join(_SEVERITY_LEVELS)}",
                exit_code=1,
            )

        if service_type and service_type not in _SERVICE_TYPES:
            return ToolResult(
                error=f"Invalid service_type '{service_type}'. Must be one of: {', '.join(_SERVICE_TYPES)}",
                exit_code=1,
            )

        params: dict[str, Any] = {
            "incident_summary": incident_summary,
            "severity": severity,
            "service_type": service_type,
            "affected_servers": affected_servers,
            "root_cause": root_cause,
            "resolution": resolution,
            "start_time": start_time,
            "end_time": end_time,
            "customer_facing": customer_facing,
        }

        prompt = _build_report_prompt(params)
        return ToolResult(output=prompt)
