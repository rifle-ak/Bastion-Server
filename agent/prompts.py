"""System prompt builder for the agent.

Assembles the system prompt dynamically from the server inventory
and registered tool list so Claude knows what it can do.
"""

from __future__ import annotations

from agent.inventory import Inventory
from agent.tools.registry import ToolRegistry


_SYSTEM_TEMPLATE = """\
You are an infrastructure management assistant for Galaxy Gaming Host.
You are running on the bastion server and have SSH access to downstream servers.

## Your Rules
1. NEVER fabricate or guess command output. Always use tools to get real data.
2. Read-only operations can be run freely. Destructive operations require operator approval.
3. If you're unsure about something, say so. Check first, act second.
4. When diagnosing issues, gather information systematically before suggesting fixes.
5. Always explain what you're about to do before doing it.
6. If a command fails, share the error output and suggest next steps.

## Available Servers
{server_inventory}

## Available Tools
{tool_list}

## Response Style
- Be direct and concise
- Lead with the answer/finding, then explain
- Use code blocks for command output
- Flag anything that looks abnormal in metrics or logs\
"""


def build_system_prompt(inventory: Inventory, registry: ToolRegistry) -> str:
    """Build the full system prompt from inventory and registered tools.

    Args:
        inventory: The server inventory.
        registry: The tool registry with all registered tools.

    Returns:
        The assembled system prompt string.
    """
    server_section = inventory.format_for_prompt()

    tool_lines: list[str] = []
    for schema in registry.get_schemas():
        name = schema["name"]
        desc = schema["description"]
        params = schema["input_schema"].get("properties", {})
        required = schema["input_schema"].get("required", [])

        param_parts: list[str] = []
        for pname, pdef in params.items():
            ptype = pdef.get("type", "any")
            pdesc = pdef.get("description", "")
            req = " (required)" if pname in required else ""
            param_parts.append(f"    - {pname} ({ptype}{req}): {pdesc}")

        param_block = "\n".join(param_parts) if param_parts else "    (no parameters)"
        tool_lines.append(f"- **{name}**: {desc}\n{param_block}")

    tool_section = "\n".join(tool_lines)

    return _SYSTEM_TEMPLATE.format(
        server_inventory=server_section,
        tool_list=tool_section,
    )
