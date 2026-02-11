"""Base tool class defining the interface all tools must implement.

Each tool provides its name, description, JSON Schema parameters, and
an async execute method. The registry uses these to generate Anthropic
tool schemas and dispatch calls.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolResult:
    """Structured result from a tool execution."""

    output: str = ""
    error: str = ""
    exit_code: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to a dict suitable for returning to the model."""
        result: dict[str, Any] = {}
        if self.output:
            result["output"] = self.output
        if self.error:
            result["error"] = self.error
        result["exit_code"] = self.exit_code
        return result

    @property
    def success(self) -> bool:
        """Whether the tool executed without error."""
        return self.exit_code == 0 and not self.error


class BaseTool(ABC):
    """Abstract base class for all agent tools."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique tool name used in API schemas and dispatch."""

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description. Claude reads this to decide when to use the tool."""

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """JSON Schema dict defining the tool's input parameters."""

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult:
        """Execute the tool with the given parameters.

        Args:
            **kwargs: Tool-specific parameters matching the JSON Schema.

        Returns:
            ToolResult with output/error and exit code.
        """

    def to_schema(self) -> dict[str, Any]:
        """Generate the Anthropic API tool schema for this tool."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                **self.parameters,
            },
        }
