"""Human-in-the-loop approval gate for destructive operations.

Checks whether a tool call requires human confirmation based on
pattern matching against the approval_required_patterns from
permissions.yaml. In interactive mode, prompts the operator via
the terminal. In auto_deny mode, all destructive operations are
refused without prompting.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from agent.config import ApprovalMode

logger = structlog.get_logger()

# Tools that are always safe (read-only, no side effects)
ALWAYS_SAFE_TOOLS: frozenset[str] = frozenset({
    "list_servers",
    "query_metrics",
})


def requires_approval(
    tool_name: str,
    tool_input: dict,
    approval_patterns: list[str],
) -> bool:
    """Determine if a tool call requires human approval.

    Args:
        tool_name: The name of the tool being called.
        tool_input: The tool's input parameters.
        approval_patterns: Patterns that trigger approval (from config).

    Returns:
        True if the operation should be confirmed by a human.
    """
    if tool_name in ALWAYS_SAFE_TOOLS:
        return False

    # Check all string values in the input against approval patterns
    values_to_check = _extract_string_values(tool_input)
    for value in values_to_check:
        value_lower = value.lower()
        for pattern in approval_patterns:
            if pattern.lower() in value_lower:
                logger.info(
                    "approval_required",
                    tool=tool_name,
                    matched_pattern=pattern,
                    matched_value=value,
                )
                return True

    return False


async def request_approval(
    tool_name: str,
    tool_input: dict,
    mode: ApprovalMode,
    console: Console | None = None,
) -> bool:
    """Request human approval for a destructive operation.

    Args:
        tool_name: The tool being called.
        tool_input: The tool's parameters.
        mode: The approval mode from config.
        console: Rich console for display (optional).

    Returns:
        True if approved, False if denied.
    """
    if mode == ApprovalMode.AUTO_DENY:
        logger.info("approval_auto_denied", tool=tool_name)
        return False

    # Interactive mode — prompt the operator
    con = console or Console()

    detail_lines = [f"  {k}: {v}" for k, v in tool_input.items()]
    detail_text = "\n".join(detail_lines)

    panel = Panel(
        Text.from_markup(
            f"[bold yellow]Tool:[/] {tool_name}\n"
            f"[bold yellow]Parameters:[/]\n{detail_text}"
        ),
        title="[bold red]Approval Required[/]",
        border_style="red",
    )
    con.print(panel)

    # Run the blocking input() in a thread so we don't block the event loop
    loop = asyncio.get_running_loop()
    try:
        response = await loop.run_in_executor(
            None,
            lambda: input("Approve this operation? [y/N]: ").strip().lower(),
        )
    except (EOFError, KeyboardInterrupt):
        con.print("[red]Approval denied (no input).[/]")
        return False

    approved = response in ("y", "yes")
    if approved:
        logger.info("approval_granted", tool=tool_name)
        con.print("[green]Approved.[/]")
    else:
        logger.info("approval_denied", tool=tool_name)
        con.print("[red]Denied.[/]")

    return approved


def _extract_string_values(obj: Any) -> list[str]:
    """Recursively extract all string values from a nested structure."""
    values: list[str] = []
    if isinstance(obj, str):
        values.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            values.extend(_extract_string_values(v))
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            values.extend(_extract_string_values(item))
    return values
