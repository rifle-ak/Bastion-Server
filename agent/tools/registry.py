"""Tool registration, schema generation, and dispatch.

Central registry that tools register with. Provides the full list of
Anthropic tool schemas for the API call and dispatches incoming tool
calls through the security pipeline to the correct implementation.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from agent.config import AgentConfig, ApprovalMode, PermissionsConfig
from agent.inventory import Inventory
from agent.security.allowlist import AllowlistDenied, check_command, check_path_read
from agent.security.approval import request_approval, requires_approval
from agent.security.audit import AuditLogger
from agent.security.sanitizer import SanitizationError, sanitize
from agent.tools.base import BaseTool, ToolResult

logger = structlog.get_logger()


class ToolRegistry:
    """Central registry for all agent tools with secure dispatch."""

    def __init__(
        self,
        config: AgentConfig,
        inventory: Inventory,
        audit: AuditLogger,
    ) -> None:
        """Initialize the registry.

        Args:
            config: Agent configuration.
            inventory: Server inventory for lookups.
            audit: Audit logger for recording all tool calls.
        """
        self._config = config
        self._inventory = inventory
        self._audit = audit
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """Register a tool instance.

        Args:
            tool: The tool to register.

        Raises:
            ValueError: If a tool with the same name is already registered.
        """
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name!r}")
        self._tools[tool.name] = tool
        logger.debug("tool_registered", tool=tool.name)

    def get_schemas(self) -> list[dict[str, Any]]:
        """Return Anthropic API tool schemas for all registered tools."""
        return [tool.to_schema() for tool in self._tools.values()]

    def get_tool(self, name: str) -> BaseTool | None:
        """Look up a tool by name."""
        return self._tools.get(name)

    @property
    def tool_names(self) -> list[str]:
        """Return all registered tool names."""
        return list(self._tools.keys())

    async def dispatch(self, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        """Execute a tool call through the full security pipeline.

        Pipeline:
        1. Sanitize inputs
        2. Log the attempt
        3. Check allowlist (for command/path tools)
        4. Check if human approval is required
        5. Execute with timeout
        6. Log the result

        Args:
            tool_name: Name of the tool to call.
            tool_input: The tool's input parameters.

        Returns:
            Dict with tool output, suitable for returning to the model.
        """
        # 0. Check tool exists
        tool = self._tools.get(tool_name)
        if tool is None:
            return {"error": f"Unknown tool: {tool_name!r}"}

        # 1. Sanitize inputs
        try:
            sanitized = sanitize(tool_name, tool_input)
        except SanitizationError as e:
            self._audit.log_denied(tool_name, tool_input, reason=f"sanitizer: {e}")
            return {"error": f"Input rejected: {e}"}

        # 2. Log the attempt
        self._audit.log_attempt(tool_name, sanitized)

        # 3. Check allowlist for command/path-bearing tools
        try:
            self._check_allowlist(tool_name, sanitized)
        except AllowlistDenied as e:
            self._audit.log_denied(tool_name, sanitized, reason=f"allowlist: {e}")
            return {"error": f"Operation not permitted by security policy: {e}"}

        # 4. Check if human approval is required
        approval_patterns = self._inventory.get_approval_patterns()
        if requires_approval(tool_name, sanitized, approval_patterns):
            approved = await request_approval(
                tool_name, sanitized, self._config.approval_mode
            )
            if not approved:
                self._audit.log_denied(tool_name, sanitized, reason="human_denied")
                return {"error": "Operation denied by operator"}

        # 5. Execute with timeout
        timeout = self._config.command_timeout
        try:
            result = await asyncio.wait_for(
                tool.execute(**sanitized),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            self._audit.log_timeout(tool_name, sanitized)
            return {"error": f"Operation timed out ({timeout}s)"}
        except Exception as e:
            self._audit.log_error(tool_name, sanitized, error=str(e))
            return {"error": f"Execution failed: {e}"}

        # 6. Log result and return
        result_dict = result.to_dict()
        if result.success:
            self._audit.log_success(tool_name, sanitized, result=result_dict)
        else:
            self._audit.log_error(
                tool_name,
                sanitized,
                error=result.error or f"exit code {result.exit_code}",
            )
        return result_dict

    def _check_allowlist(self, tool_name: str, tool_input: dict[str, Any]) -> None:
        """Run allowlist checks for tools that involve commands or paths.

        Args:
            tool_name: The tool name.
            tool_input: The sanitized input.

        Raises:
            AllowlistDenied: If the operation is not permitted.
        """
        server_name = tool_input.get("server")

        # Tools with a 'command' field need command allowlist checks
        if "command" in tool_input and server_name:
            server_info = self._inventory.get_server(server_name)
            check_command(
                tool_input["command"],
                server_info.definition.role,
                server_info.permissions,
            )
        elif "command" in tool_input:
            # Local commands use the bastion role
            try:
                server_info = self._inventory.get_server("localhost")
            except KeyError:
                raise AllowlistDenied(
                    tool_input["command"],
                    "bastion (no 'localhost' entry in server inventory)",
                )
            check_command(
                tool_input["command"],
                server_info.definition.role,
                server_info.permissions,
            )

        # Tools with a 'path' field need path read checks
        if "path" in tool_input and server_name:
            server_info = self._inventory.get_server(server_name)
            check_path_read(
                tool_input["path"],
                server_info.definition.role,
                server_info.permissions,
            )
