"""Client-facing explanation generator.

After diagnosing an issue, this tool generates a non-technical
explanation suitable for pasting into a support ticket or client
message. Saves the operator time composing responses.

Also generates a shift handoff summary of what happened during
the current session.
"""

from __future__ import annotations

from typing import Any

from agent.tools.base import BaseTool, ToolResult


class ExplainToClient(BaseTool):
    """Generate a client-friendly explanation of a technical issue."""

    @property
    def name(self) -> str:
        return "explain_to_client"

    @property
    def description(self) -> str:
        return (
            "Generate a non-technical, client-friendly explanation of a "
            "technical issue. Input: the technical diagnosis. Output: a "
            "clear, reassuring message you can paste into a support ticket. "
            "Avoids jargon, explains impact and resolution."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "diagnosis": {
                    "type": "string",
                    "description": "The technical diagnosis/findings to explain.",
                },
                "service_type": {
                    "type": "string",
                    "description": "Type of service: 'game_server', 'website', 'email', 'general'.",
                    "default": "general",
                },
                "tone": {
                    "type": "string",
                    "description": "Tone: 'professional', 'friendly', 'brief'.",
                    "default": "professional",
                },
            },
            "required": ["diagnosis"],
        }

    async def execute(
        self, *, diagnosis: str, service_type: str = "general", tone: str = "professional", **kwargs: Any,
    ) -> ToolResult:
        """Generate client explanation.

        Note: This tool provides a structured template. The actual
        natural language generation is done by Claude using this output
        as context — the tool just formats and structures the request.
        """
        return ToolResult(output=_build_explanation_prompt(diagnosis, service_type, tone))


class ShiftHandoff(BaseTool):
    """Generate a shift handoff summary."""

    @property
    def name(self) -> str:
        return "shift_handoff"

    @property
    def description(self) -> str:
        return (
            "Generate a shift handoff summary of what was investigated "
            "and resolved during this session. Include: issues found, "
            "actions taken, pending items. Suitable for team chat."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "actions": {
                    "type": "string",
                    "description": "Summary of actions taken during this session.",
                },
                "pending": {
                    "type": "string",
                    "description": "Any pending items or follow-ups.",
                    "default": "",
                },
            },
            "required": ["actions"],
        }

    async def execute(self, *, actions: str, pending: str = "", **kwargs: Any) -> ToolResult:
        """Generate handoff summary."""
        return ToolResult(output=_build_handoff(actions, pending))


def _build_explanation_prompt(diagnosis: str, service_type: str, tone: str) -> str:
    """Build a structured prompt for client explanation generation."""
    type_context = {
        "game_server": (
            "The client runs a game server (Minecraft, Rust, ARK, etc.). "
            "They care about: lag, disconnections, server uptime, player experience. "
            "Use terms they understand: 'lag', 'server performance', 'player slots'."
        ),
        "website": (
            "The client has a website. They care about: page load speed, "
            "uptime, SEO impact, visitor experience. "
            "Use terms they understand: 'page speed', 'loading time', 'visitors'."
        ),
        "email": (
            "The client has email issues. They care about: sending/receiving, "
            "spam filtering, delivery reliability. "
            "Use terms they understand: 'inbox', 'delivery', 'spam filter'."
        ),
        "general": "Explain in plain language without technical jargon.",
    }

    tone_guidance = {
        "professional": "Professional and reassuring. Use 'we' language.",
        "friendly": "Warm and approachable. First-name basis feel.",
        "brief": "Short and direct. 2-3 sentences max.",
    }

    context = type_context.get(service_type, type_context["general"])
    tone_guide = tone_guidance.get(tone, tone_guidance["professional"])

    return (
        f"## Client Explanation Request\n\n"
        f"**Context:** {context}\n"
        f"**Tone:** {tone_guide}\n\n"
        f"**Technical Diagnosis:**\n{diagnosis}\n\n"
        f"**Instructions:** Rewrite the above diagnosis as a client-facing "
        f"message. Include:\n"
        f"1. What was happening (in their terms)\n"
        f"2. What caused it (simplified)\n"
        f"3. What was done to fix it\n"
        f"4. What to expect going forward\n\n"
        f"Do NOT include: server names, IP addresses, technical commands, "
        f"file paths, or internal infrastructure details."
    )


def _build_handoff(actions: str, pending: str) -> str:
    """Build a shift handoff template."""
    import time
    from datetime import datetime

    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = [
        f"## Shift Handoff — {now}\n",
        "**Actions taken:**",
        actions,
    ]

    if pending:
        lines.extend([
            "",
            "**Pending / Follow-up:**",
            pending,
        ])

    lines.extend([
        "",
        "---",
        "_Generated by Bastion Agent_",
    ])

    return "\n".join(lines)
