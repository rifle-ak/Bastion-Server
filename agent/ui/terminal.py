"""Rich-based terminal interface for the bastion agent.

Handles user input, displays assistant responses, and shows tool
call details with clear visual formatting.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text


class TerminalUI:
    """Interactive terminal UI using Rich."""

    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console()

    def display_banner(self, version: str, model: str, servers: list[str]) -> None:
        """Show the startup banner."""
        banner = Text()
        banner.append("Bastion Agent", style="bold cyan")
        banner.append(f" v{version}\n", style="dim")
        banner.append(f"Model: {model}\n", style="")
        banner.append(f"Servers: {', '.join(servers)}\n", style="")
        banner.append("Type /quit or /exit to end the session.", style="dim")

        self._console.print(Panel(banner, border_style="cyan", title="Galaxy Gaming Host"))
        self._console.print()

    async def get_input(self) -> str:
        """Prompt the user for input, running in a thread to avoid blocking.

        Returns:
            The user's input string, stripped.
        """
        loop = asyncio.get_running_loop()
        try:
            raw = await loop.run_in_executor(
                None,
                lambda: input("\n[bastion] > "),
            )
            return raw.strip()
        except (EOFError, KeyboardInterrupt):
            return "/quit"

    def display_response(self, text: str) -> None:
        """Display Claude's text response as rendered markdown."""
        self._console.print()
        self._console.print(Markdown(text))

    def display_tool_call(self, tool_name: str, tool_input: dict[str, Any]) -> None:
        """Display a tool call being made."""
        input_str = json.dumps(tool_input, indent=2)
        syntax = Syntax(input_str, "json", theme="monokai", line_numbers=False)

        panel = Panel(
            syntax,
            title=f"[bold yellow]Tool Call:[/] {tool_name}",
            border_style="yellow",
            padding=(0, 1),
        )
        self._console.print(panel)

    def display_tool_result(self, tool_name: str, result: dict[str, Any]) -> None:
        """Display the result of a tool call."""
        output = result.get("output", "")
        error = result.get("error", "")
        exit_code = result.get("exit_code", 0)

        if error and not output:
            # Error-only result
            self._console.print(
                Panel(
                    Text(error, style="red"),
                    title=f"[bold red]Error:[/] {tool_name}",
                    border_style="red",
                    padding=(0, 1),
                )
            )
        else:
            # Truncate very long output for display (full output goes to Claude)
            display_output = output
            if len(display_output) > 3000:
                display_output = display_output[:3000] + f"\n... ({len(output)} chars total)"

            style = "green" if exit_code == 0 else "yellow"
            title_prefix = "Result" if exit_code == 0 else f"Result (exit {exit_code})"

            content = Text(display_output)
            if error:
                content.append(f"\nstderr: {error}", style="dim red")

            self._console.print(
                Panel(
                    content,
                    title=f"[bold {style}]{title_prefix}:[/] {tool_name}",
                    border_style=style,
                    padding=(0, 1),
                )
            )

    def display_error(self, message: str) -> None:
        """Display an error message."""
        self._console.print(f"[bold red]Error:[/] {message}")

    def display_info(self, message: str) -> None:
        """Display an informational message."""
        self._console.print(f"[dim]{message}[/]")

    def display_goodbye(self) -> None:
        """Show session end message."""
        self._console.print("\n[dim]Session ended. Goodbye.[/]")
