"""Anthropic API client and conversation loop.

Manages the message history, sends requests to the Claude API with
tool definitions, and processes tool_use responses by dispatching
through the tool registry.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import anthropic
import structlog

from agent.config import AgentConfig
from agent.tools.registry import ToolRegistry

logger = structlog.get_logger()

# Max characters to keep per tool result in message history.
# The user still sees the full output — this only affects what we
# send back to the API on subsequent turns to control token usage.
_MAX_TOOL_RESULT_CHARS = 3000

_RATE_LIMIT_MAX_RETRIES = 3
_RATE_LIMIT_BASE_DELAY = 2.0  # seconds

# Rough chars-per-token ratio for estimating token counts.
# English text averages ~4 chars/token; JSON/code is closer to 3.
_CHARS_PER_TOKEN = 3.5


class CancelledByUser(Exception):
    """Raised when the user cancels the current operation."""


class ConversationClient:
    """Manages the conversation loop between the user, Claude, and tools."""

    def __init__(
        self,
        config: AgentConfig,
        registry: ToolRegistry,
        system_prompt: str,
        ui: Any,
    ) -> None:
        """Initialize the conversation client.

        Args:
            config: Agent configuration (model, max_tokens, etc.).
            registry: Tool registry for dispatch and schema generation.
            system_prompt: The assembled system prompt.
            ui: UI instance (TerminalUI or DaemonUI) — must implement
                get_input, display_response, display_tool_call,
                display_tool_result, display_error, display_goodbye.
        """
        self._config = config
        self._registry = registry
        self._system_prompt = system_prompt
        self._ui = ui
        self._client = anthropic.Anthropic()
        self._messages: list[dict[str, Any]] = []
        self._cancel_event: asyncio.Event | None = None

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

    async def process_message(self, message: str) -> None:
        """Process a single user message and generate a response.

        Used by daemon mode where the outer session loop is managed
        externally rather than by the interactive ``run()`` loop.

        Args:
            message: The user's message text.
        """
        self._messages.append({"role": "user", "content": message})
        await self._process_response()

    def reset(self) -> None:
        """Clear conversation history (called between daemon sessions)."""
        self._messages.clear()

    def get_messages(self) -> list[dict[str, Any]]:
        """Return a copy of the current message history."""
        return list(self._messages)

    def restore_messages(self, messages: list[dict[str, Any]]) -> None:
        """Replace the message history with a previously saved one.

        Used by the daemon to resume a session from disk.
        """
        self._messages.clear()
        self._messages.extend(messages)

    def set_cancel_event(self, event: asyncio.Event) -> None:
        """Set an external cancellation event.

        When this event is set, the conversation loop will stop after the
        current API call or tool execution completes.
        """
        self._cancel_event = event

    def _is_cancelled(self) -> bool:
        """Check if the current operation has been cancelled."""
        return self._cancel_event is not None and self._cancel_event.is_set()

    async def _process_response(self) -> None:
        """Send messages to Claude and handle the response.

        Loops through tool_use rounds until Claude produces a final
        text response (stop_reason == "end_turn"), respecting the
        max_tool_iterations safety limit.  Checks for cancellation
        between each iteration.
        """
        iterations = 0

        while iterations < self._config.max_tool_iterations:
            iterations += 1

            # Check for cancellation before each round
            if self._is_cancelled():
                logger.info("operation_cancelled", iteration=iterations)
                raise CancelledByUser()

            try:
                response = await self._api_call_with_retry()
            except CancelledByUser:
                raise
            except anthropic.APIError as e:
                self._ui.display_error(f"API error: {e}")
                logger.error("api_error", error=str(e))
                # Remove the last user message so they can retry
                self._messages.pop()
                return

            # Append assistant response to history.
            # Convert content blocks to plain dicts to avoid pydantic
            # serialization issues when they're passed back in subsequent
            # API calls.
            assistant_content = response.content
            serialized_content = [
                block.model_dump() if hasattr(block, "model_dump") else block
                for block in assistant_content
            ]
            self._messages.append({"role": "assistant", "content": serialized_content})

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
                    # Check cancellation before each tool execution
                    if self._is_cancelled():
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": "Operation cancelled by user.",
                            "is_error": True,
                        })
                        continue

                    self._ui.display_tool_call(block.name, block.input)
                    result = await self._registry.dispatch(block.name, block.input)
                    self._ui.display_tool_result(block.name, result)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": _truncate_tool_result(json.dumps(result)),
                    })
                elif hasattr(block, "text") and block.text:
                    # Claude may include thinking text alongside tool calls
                    self._ui.display_response(block.text)

            # If cancelled during tool execution, stop the loop
            if self._is_cancelled():
                # Still append the partial results so history stays valid
                self._messages.append({"role": "user", "content": tool_results})
                raise CancelledByUser()

            # Append tool results for the next iteration
            self._messages.append({"role": "user", "content": tool_results})

        # Safety: hit max iterations
        self._ui.display_error(
            f"Reached maximum tool iterations ({self._config.max_tool_iterations}). "
            "Stopping to prevent runaway loops."
        )
        logger.warning("max_tool_iterations_reached", limit=self._config.max_tool_iterations)

    def _trim_history(self) -> None:
        """Drop oldest message pairs when the conversation exceeds the token budget.

        Preserves the most recent messages so Claude keeps context for the
        current task.  Always keeps at least the last user message + the
        preceding assistant turn (if any) so the current exchange is intact.

        Messages must be dropped in valid pairs to keep the alternating
        user/assistant structure the API requires:
        - user (text) + assistant
        - user (tool_results) + assistant
        """
        budget = self._config.max_conversation_tokens
        est = _estimate_tokens(self._messages)
        if est <= budget:
            return

        # Keep removing the oldest pair until we're under budget.
        # Never remove the last 2 messages (current turn).
        removed = 0
        while est > budget and len(self._messages) > 2:
            # Remove from the front: one user + one assistant = 2 messages
            if len(self._messages) <= 2:
                break
            self._messages.pop(0)
            removed += 1
            # If the new front is an assistant message, remove it too
            # to keep user/assistant alternation valid
            if self._messages and self._messages[0].get("role") == "assistant":
                self._messages.pop(0)
                removed += 1
            est = _estimate_tokens(self._messages)

        if removed:
            logger.info(
                "history_trimmed",
                removed_messages=removed,
                remaining=len(self._messages),
                est_tokens=est,
            )

    async def _api_call_with_retry(self) -> anthropic.types.Message:
        """Call the Anthropic API with retry on rate limit errors.

        The synchronous Anthropic SDK call is run in a thread executor so
        the asyncio event loop stays responsive — this allows the daemon
        to detect client disconnection (cancel) while waiting for the API.
        """
        self._trim_history()
        loop = asyncio.get_running_loop()

        def _sync_create() -> anthropic.types.Message:
            return self._client.messages.create(
                model=self._config.model,
                max_tokens=self._config.max_tokens,
                system=self._system_prompt,
                tools=self._registry.get_schemas(),
                messages=self._messages,
            )

        for attempt in range(_RATE_LIMIT_MAX_RETRIES + 1):
            try:
                api_future = loop.run_in_executor(None, _sync_create)

                # Race the API call against the cancel event (if set)
                if self._cancel_event is not None:
                    cancel_future = asyncio.ensure_future(self._cancel_event.wait())
                    done, pending = await asyncio.wait(
                        {asyncio.ensure_future(api_future), cancel_future},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for p in pending:
                        p.cancel()
                    if self._cancel_event.is_set():
                        raise CancelledByUser()
                    return done.pop().result()
                else:
                    return await api_future
            except CancelledByUser:
                raise
            except anthropic.RateLimitError:
                if attempt >= _RATE_LIMIT_MAX_RETRIES:
                    raise
                delay = _RATE_LIMIT_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "rate_limited",
                    attempt=attempt + 1,
                    retry_in=delay,
                )
                self._ui.display_error(
                    f"Rate limited — retrying in {delay:.0f}s "
                    f"(attempt {attempt + 1}/{_RATE_LIMIT_MAX_RETRIES})"
                )
                await asyncio.sleep(delay)
        # unreachable, but keeps type checkers happy
        raise RuntimeError("retry loop exited unexpectedly")


def _truncate_tool_result(content: str) -> str:
    """Truncate a tool result string for message history.

    The user sees the full output in the terminal. This only limits
    what gets sent back to the API to stay within token budgets.
    """
    if len(content) <= _MAX_TOOL_RESULT_CHARS:
        return content
    half = _MAX_TOOL_RESULT_CHARS // 2
    return (
        content[:half]
        + f"\n\n... ({len(content) - _MAX_TOOL_RESULT_CHARS} chars truncated) ...\n\n"
        + content[-half:]
    )


def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Rough token estimate for a message list.

    Counts total characters across all message content and divides by the
    average chars-per-token ratio.  Not exact, but good enough for
    deciding when to trim.
    """
    total_chars = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total_chars += len(json.dumps(block, default=str))
                elif isinstance(block, str):
                    total_chars += len(block)
    return int(total_chars / _CHARS_PER_TOKEN)
