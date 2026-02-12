"""Tests for tool base class, registry, and tool dispatch."""

from __future__ import annotations

import asyncio
import tempfile
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from agent.config import (
    AgentConfig,
    PermissionsConfig,
    RolePermissions,
    ServerDefinition,
    ServersConfig,
)
from agent.inventory import Inventory
from agent.security.audit import AuditLogger
from agent.tools.base import BaseTool, ToolResult
from agent.tools.registry import ToolRegistry


# --- Fixtures ---


class DummyTool(BaseTool):
    """A simple test tool."""

    @property
    def name(self) -> str:
        return "dummy_tool"

    @property
    def description(self) -> str:
        return "A test tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"properties": {"msg": {"type": "string"}}, "required": ["msg"]}

    async def execute(self, *, msg: str, **kwargs: Any) -> ToolResult:
        return ToolResult(output=f"echo: {msg}", exit_code=0)


class FailingTool(BaseTool):
    """A tool that always raises."""

    @property
    def name(self) -> str:
        return "failing_tool"

    @property
    def description(self) -> str:
        return "Always fails"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> ToolResult:
        raise RuntimeError("Intentional failure")


class SlowTool(BaseTool):
    """A tool that takes too long."""

    @property
    def name(self) -> str:
        return "slow_tool"

    @property
    def description(self) -> str:
        return "Takes forever"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> ToolResult:
        await asyncio.sleep(100)
        return ToolResult(output="done")


@pytest.fixture
def agent_config() -> AgentConfig:
    return AgentConfig(command_timeout=2)  # Short timeout for tests


@pytest.fixture
def inventory() -> Inventory:
    servers = ServersConfig(
        servers={
            "localhost": ServerDefinition(
                host="localhost", role="bastion", ssh=False
            ),
        }
    )
    perms = PermissionsConfig(
        roles={
            "bastion": RolePermissions(
                allowed_commands=["uptime", "df -h"],
                allowed_paths_read=["/var/log/"],
            ),
        },
        approval_required_patterns=["restart"],
    )
    return Inventory(servers, perms)


@pytest.fixture
def audit_logger():
    tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
    logger = AuditLogger(tmp.name)
    yield logger
    logger.close()


@pytest.fixture
def registry(agent_config, inventory, audit_logger) -> ToolRegistry:
    return ToolRegistry(agent_config, inventory, audit_logger)


# --- Tests ---


class TestToolResult:
    """Tests for ToolResult."""

    def test_success(self):
        r = ToolResult(output="ok", exit_code=0)
        assert r.success is True

    def test_failure_exit_code(self):
        r = ToolResult(output="", exit_code=1)
        assert r.success is False

    def test_failure_error(self):
        r = ToolResult(error="bad", exit_code=0)
        assert r.success is False

    def test_to_dict(self):
        r = ToolResult(output="hello", error="warn", exit_code=0)
        d = r.to_dict()
        assert d["output"] == "hello"
        assert d["error"] == "warn"
        assert d["exit_code"] == 0

    def test_to_dict_omits_empty_error(self):
        """output is always present; error is omitted when empty."""
        r = ToolResult(exit_code=0)
        d = r.to_dict()
        assert d["output"] == ""
        assert "error" not in d


class TestToolSchema:
    """Tests for schema generation."""

    def test_schema_format(self):
        tool = DummyTool()
        schema = tool.to_schema()
        assert schema["name"] == "dummy_tool"
        assert schema["description"] == "A test tool"
        assert schema["input_schema"]["type"] == "object"
        assert "msg" in schema["input_schema"]["properties"]


class TestToolRegistry:
    """Tests for tool registration and dispatch."""

    def test_register(self, registry):
        registry.register(DummyTool())
        assert "dummy_tool" in registry.tool_names

    def test_register_duplicate_raises(self, registry):
        registry.register(DummyTool())
        with pytest.raises(ValueError, match="already registered"):
            registry.register(DummyTool())

    def test_get_schemas(self, registry):
        registry.register(DummyTool())
        schemas = registry.get_schemas()
        assert len(schemas) == 1
        assert schemas[0]["name"] == "dummy_tool"

    def test_get_tool(self, registry):
        tool = DummyTool()
        registry.register(tool)
        assert registry.get_tool("dummy_tool") is tool
        assert registry.get_tool("nonexistent") is None


class TestToolDispatch:
    """Tests for the dispatch pipeline."""

    @pytest.mark.asyncio
    async def test_successful_dispatch(self, registry):
        registry.register(DummyTool())
        result = await registry.dispatch("dummy_tool", {"msg": "hello"})
        assert result["output"] == "echo: hello"
        assert result["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_unknown_tool(self, registry):
        result = await registry.dispatch("nonexistent", {})
        assert "error" in result
        assert "Unknown tool" in result["error"]

    @pytest.mark.asyncio
    async def test_sanitizer_blocks_injection(self, registry):
        registry.register(DummyTool())
        result = await registry.dispatch("dummy_tool", {"msg": "ok", "command": "ls; rm /"})
        assert "error" in result
        assert "rejected" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_execution_error_handled(self, registry):
        registry.register(FailingTool())
        result = await registry.dispatch("failing_tool", {})
        assert "error" in result
        assert "Intentional failure" in result["error"]

    @pytest.mark.asyncio
    async def test_timeout_handled(self, registry):
        registry.register(SlowTool())
        result = await registry.dispatch("slow_tool", {})
        assert "error" in result
        assert "timed out" in result["error"]
