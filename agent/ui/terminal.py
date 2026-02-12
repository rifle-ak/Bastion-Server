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
        """Display a tool call being made — compact one-liner for simple inputs."""
        if tool_input and all(isinstance(v, (str, int, bool)) for v in tool_input.values()):
            # Build with Text to avoid Rich markup parsing of user values
            text = Text("  ")
            text.append("▶", style="yellow")
            text.append(" ")
            text.append(tool_name, style="bold")
            text.append("  ")
            for i, (k, v) in enumerate(tool_input.items()):
                if i > 0:
                    text.append(" ")
                text.append(f"{k}=", style="dim")
                text.append(str(v))
            self._console.print(text)
        else:
            # Fall back to JSON for complex inputs
            input_str = json.dumps(tool_input, indent=2)
            syntax = Syntax(input_str, "json", theme="monokai", line_numbers=False)
            self._console.print(
                Panel(
                    syntax,
                    title=f"[bold yellow]▶ {tool_name}[/]",
                    border_style="yellow",
                    padding=(0, 1),
                )
            )

    def display_tool_result(self, tool_name: str, result: dict[str, Any]) -> None:
        """Display the result of a tool call."""
        output = result.get("output", "")
        error = result.get("error", "")
        exit_code = result.get("exit_code", 0)

        if error and not output:
            # Error-only: compact one-liner (use Text to avoid markup parsing)
            text = Text("  ")
            text.append("✗", style="red")
            text.append(" ")
            text.append(f"{tool_name}: ", style="bold")
            text.append(error, style="red")
            self._console.print(text)
        elif not output and not error:
            # Empty result
            text = Text("  ")
            text.append("✓", style="green")
            text.append(" ")
            text.append(tool_name, style="bold")
            text.append(" (no output)", style="dim")
            self._console.print(text)
        else:
            # Truncate very long output for display
            display_output = output
            truncated = len(output) > 3000
            if truncated:
                display_output = output[:3000]

            style = "green" if exit_code == 0 else "yellow"
            icon = "✓" if exit_code == 0 else "⚠"

            content = Text(display_output)
            if truncated:
                content.append(
                    f"\n\n─── truncated ({len(output):,} chars total) ───",
                    style="dim",
                )
            if error:
                content.append(f"\nstderr: {error}", style="dim red")

            self._console.print(
                Panel(
                    content,
                    title=f"[bold {style}]{icon} {tool_name}[/]",
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
