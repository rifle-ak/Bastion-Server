"""Anthropic API client and conversation loop.

Manages the message history, sends requests to the Claude API with
tool definitions, and processes tool_use responses by dispatching
through the tool registry.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic
import structlog

from agent.config import AgentConfig
from agent.tools.registry import ToolRegistry
from agent.ui.terminal import TerminalUI

logger = structlog.get_logger()


class ConversationClient:
    """Manages the conversation loop between the user, Claude, and tools."""

    def __init__(
        self,
        config: AgentConfig,
        registry: ToolRegistry,
        system_prompt: str,
        ui: TerminalUI,
    ) -> None:
        """Initialize the conversation client.

        Args:
            config: Agent configuration (model, max_tokens, etc.).
            registry: Tool registry for dispatch and schema generation.
            system_prompt: The assembled system prompt.
            ui: Terminal UI for display.
        """
        self._config = config
        self._registry = registry
        self._system_prompt = system_prompt
        self._ui = ui
        self._client = anthropic.Anthropic()
        self._messages: list[dict[str, Any]] = []

    async def run(self) -> None:
        """Run the interactive conversation loop.

        Loops until the user types /quit or /exit. Each user message
        may trigger multiple rounds of tool calls before Claude
        produces a final text response.
        """
        while True:
            user_input = await self._ui.get_input()

            if user_input in ("/quit", "/exit"):
                self._ui.display_goodbye()
                break

            if not user_input:
                continue

            self._messages.append({"role": "user", "content": user_input})

            await self._process_response()

    async def _process_response(self) -> None:
        """Send messages to Claude and handle the response.

        Loops through tool_use rounds until Claude produces a final
        text response (stop_reason == "end_turn"), respecting the
        max_tool_iterations safety limit.
        """
        iterations = 0

        while iterations < self._config.max_tool_iterations:
            iterations += 1

            try:
                response = self._client.messages.create(
                    model=self._config.model,
                    max_tokens=self._config.max_tokens,
                    system=self._system_prompt,
                    tools=self._registry.get_schemas(),
                    messages=self._messages,
                )
            except anthropic.APIError as e:
                self._ui.display_error(f"API error: {e}")
                logger.error("api_error", error=str(e))
                # Remove the last user message so they can retry
                self._messages.pop()
                return

            # Append assistant response to history
            assistant_content = response.content
            self._messages.append({"role": "assistant", "content": assistant_content})

            # If Claude is done talking (no more tool calls), display and return
            if response.stop_reason == "end_turn":
                for block in assistant_content:
                    if hasattr(block, "text"):
                        self._ui.display_response(block.text)
                return

            # Process tool calls
            tool_results: list[dict[str, Any]] = []
            for block in assistant_content:
                if block.type == "tool_use":
                    self._ui.display_tool_call(block.name, block.input)
                    result = await self._registry.dispatch(block.name, block.input)
                    self._ui.display_tool_result(block.name, result)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    })
                elif hasattr(block, "text") and block.text:
                    # Claude may include thinking text alongside tool calls
                    self._ui.display_response(block.text)

            # Append tool results for the next iteration
            self._messages.append({"role": "user", "content": tool_results})

        # Safety: hit max iterations
        self._ui.display_error(
            f"Reached maximum tool iterations ({self._config.max_tool_iterations}). "
            "Stopping to prevent runaway loops."
        )
        logger.warning("max_tool_iterations_reached", limit=self._config.max_tool_iterations)
