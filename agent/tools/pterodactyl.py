"""Pterodactyl Panel API tools.

Interfaces with the Pterodactyl Panel HTTP API to manage game servers:
list servers, check power/resource status, send console commands, and
view console output. This complements the SSH-based Wings tools by
providing panel-level visibility.

Requires a Pterodactyl API key configured in the server definition's
``panel_url`` and ``panel_api_key`` fields, or via environment variables
``PTERODACTYL_URL`` and ``PTERODACTYL_API_KEY``.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import structlog

from agent.inventory import Inventory
from agent.tools.base import BaseTool, ToolResult

logger = structlog.get_logger()

_TIMEOUT = 15


def _get_panel_config(inventory: Inventory, server: str) -> tuple[str, str] | None:
    """Get panel URL and API key from server config or environment.

    Returns (url, api_key) or None if not configured.
    """
    url = os.environ.get("PTERODACTYL_URL", "")
    api_key = os.environ.get("PTERODACTYL_API_KEY", "")

    if url and api_key:
        return url.rstrip("/"), api_key
    return None


def _panel_get(url: str, api_key: str, endpoint: str) -> dict[str, Any]:
    """Make a GET request to the Pterodactyl Panel API."""
    full_url = f"{url}/api/application/{endpoint}"
    req = Request(full_url)
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/json")

    try:
        with urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        body = e.read().decode() if e.fp else ""
        raise RuntimeError(f"Panel API {e.code}: {body[:200]}") from e
    except URLError as e:
        raise RuntimeError(f"Panel unreachable: {e.reason}") from e


def _panel_client_get(url: str, api_key: str, endpoint: str) -> dict[str, Any]:
    """Make a GET request to the Pterodactyl Client API."""
    full_url = f"{url}/api/client/{endpoint}"
    req = Request(full_url)
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/json")

    try:
        with urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        body = e.read().decode() if e.fp else ""
        raise RuntimeError(f"Panel API {e.code}: {body[:200]}") from e
    except URLError as e:
        raise RuntimeError(f"Panel unreachable: {e.reason}") from e


def _panel_post(url: str, api_key: str, endpoint: str, data: dict | None = None) -> dict[str, Any] | None:
    """Make a POST request to the Pterodactyl Client API."""
    full_url = f"{url}/api/client/{endpoint}"
    body = json.dumps(data or {}).encode()
    req = Request(full_url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/json")

    try:
        with urlopen(req, timeout=_TIMEOUT) as resp:
            content = resp.read().decode()
            return json.loads(content) if content.strip() else None
    except HTTPError as e:
        body_text = e.read().decode() if e.fp else ""
        if e.code == 204:
            return None  # No content = success
        raise RuntimeError(f"Panel API {e.code}: {body_text[:200]}") from e
    except URLError as e:
        raise RuntimeError(f"Panel unreachable: {e.reason}") from e


class PterodactylListServers(BaseTool):
    """List all game servers managed by the Pterodactyl Panel."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "pterodactyl_list_servers"

    @property
    def description(self) -> str:
        return (
            "List all game servers on the Pterodactyl Panel with name, "
            "status, node, owner, and resource limits."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Game server host (used to find panel config).",
                },
            },
            "required": ["server"],
        }

    async def execute(self, *, server: str, **kwargs: Any) -> ToolResult:
        """List all servers via Panel API."""
        config = _get_panel_config(self._inventory, server)
        if not config:
            return ToolResult(
                error="Pterodactyl Panel not configured. Set PTERODACTYL_URL and PTERODACTYL_API_KEY.",
                exit_code=1,
            )
        url, api_key = config

        try:
            data = _panel_get(url, api_key, "servers?include=node,allocations")
        except RuntimeError as e:
            return ToolResult(error=str(e), exit_code=1)

        servers = data.get("data", [])
        if not servers:
            return ToolResult(output="No servers found on the panel.")

        lines: list[str] = [
            f"{'ID':<6} {'Name':<25} {'Status':<12} {'Node':<15} "
            f"{'CPU':<6} {'RAM':<8} {'Disk':<8} {'UUID (short)'}"
        ]
        lines.append("-" * 100)

        for s in servers:
            attrs = s.get("attributes", {})
            sid = attrs.get("id", "?")
            name = attrs.get("name", "?")[:24]
            suspended = attrs.get("suspended", False)
            status = "SUSPENDED" if suspended else "active"
            uuid = attrs.get("uuid", "?")[:8]

            # Resource limits
            limits = attrs.get("limits", {})
            cpu = f"{limits.get('cpu', '?')}%"
            ram = f"{limits.get('memory', '?')}MB"
            disk = f"{limits.get('disk', '?')}MB"

            # Node name from relationships
            node_name = "?"
            rels = attrs.get("relationships", {})
            node_data = rels.get("node", {}).get("attributes", {})
            if node_data:
                node_name = node_data.get("name", "?")[:14]

            lines.append(
                f"{sid:<6} {name:<25} {status:<12} {node_name:<15} "
                f"{cpu:<6} {ram:<8} {disk:<8} {uuid}"
            )

        lines.append(f"\nTotal: {len(servers)} servers")
        return ToolResult(output="\n".join(lines))


class PterodactylServerStatus(BaseTool):
    """Get detailed status and resource usage for a game server."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "pterodactyl_server_status"

    @property
    def description(self) -> str:
        return (
            "Get live resource usage (CPU, RAM, disk, network, uptime) and "
            "power state for a Pterodactyl game server."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Game server host.",
                },
                "identifier": {
                    "type": "string",
                    "description": "Panel server ID or UUID (short form OK).",
                },
            },
            "required": ["server", "identifier"],
        }

    async def execute(self, *, server: str, identifier: str, **kwargs: Any) -> ToolResult:
        """Get server resource usage via Client API."""
        config = _get_panel_config(self._inventory, server)
        if not config:
            return ToolResult(error="Panel not configured.", exit_code=1)
        url, api_key = config

        try:
            resources = _panel_client_get(url, api_key, f"servers/{identifier}/resources")
        except RuntimeError as e:
            return ToolResult(error=str(e), exit_code=1)

        attrs = resources.get("attributes", {})
        state = attrs.get("current_state", "unknown")
        is_suspended = attrs.get("is_suspended", False)
        res = attrs.get("resources", {})

        cpu = res.get("cpu_absolute", 0)
        ram_bytes = res.get("memory_bytes", 0)
        ram_mb = ram_bytes / (1024 * 1024) if ram_bytes else 0
        disk_bytes = res.get("disk_bytes", 0)
        disk_mb = disk_bytes / (1024 * 1024) if disk_bytes else 0
        net_rx = res.get("network_rx_bytes", 0) / (1024 * 1024)
        net_tx = res.get("network_tx_bytes", 0) / (1024 * 1024)
        uptime_ms = res.get("uptime", 0)
        uptime_h = uptime_ms / (1000 * 3600) if uptime_ms else 0

        lines: list[str] = [f"**Server:** {identifier}"]
        status_icon = "✓" if state == "running" else "✗" if state == "offline" else "⚠"
        lines.append(f"**State:** {status_icon} {state}")
        if is_suspended:
            lines.append("**⚠ SUSPENDED**")
        lines.append(f"**CPU:** {cpu:.1f}%")
        lines.append(f"**RAM:** {ram_mb:.0f} MB")
        lines.append(f"**Disk:** {disk_mb:.0f} MB")
        lines.append(f"**Network:** ↓{net_rx:.1f} MB / ↑{net_tx:.1f} MB")
        if uptime_h > 0:
            lines.append(f"**Uptime:** {uptime_h:.1f} hours")

        return ToolResult(output="\n".join(lines))


class PterodactylPowerAction(BaseTool):
    """Send a power action to a game server (start/stop/restart/kill)."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "pterodactyl_power"

    @property
    def description(self) -> str:
        return (
            "Send a power action (start, stop, restart, kill) to a "
            "Pterodactyl game server. REQUIRES OPERATOR APPROVAL."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Game server host.",
                },
                "identifier": {
                    "type": "string",
                    "description": "Panel server ID or UUID.",
                },
                "action": {
                    "type": "string",
                    "description": "Power action: 'start', 'stop', 'restart', or 'kill'.",
                    "enum": ["start", "stop", "restart", "kill"],
                },
            },
            "required": ["server", "identifier", "action"],
        }

    async def execute(self, *, server: str, identifier: str, action: str, **kwargs: Any) -> ToolResult:
        """Send power action via Client API."""
        if action not in ("start", "stop", "restart", "kill"):
            return ToolResult(error=f"Invalid action: {action}", exit_code=1)

        config = _get_panel_config(self._inventory, server)
        if not config:
            return ToolResult(error="Panel not configured.", exit_code=1)
        url, api_key = config

        try:
            _panel_post(url, api_key, f"servers/{identifier}/power", {"signal": action})
        except RuntimeError as e:
            return ToolResult(error=str(e), exit_code=1)

        return ToolResult(output=f"Power action '{action}' sent to server {identifier}.")


class PterodactylConsoleCommand(BaseTool):
    """Send a command to a game server's console via the Panel API.

    Commands are checked against a game-aware allowlist that auto-detects
    the game type (Minecraft, Rust, CS2, etc.) and classifies commands as
    safe (no approval), needs-approval, or blocked. This prevents
    accidental server shutdowns via console while allowing safe read-only
    commands like ``list`` or ``status`` to run freely.

    The allowlist evolves: custom rules in config/console_commands.yaml
    override built-in defaults without code changes.
    """

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "pterodactyl_command"

    @property
    def description(self) -> str:
        return (
            "Send a console command to a running game server via the "
            "Pterodactyl Panel API. Safe commands (list, status, tps) run "
            "freely. Destructive commands need approval. Server stop/quit "
            "commands are blocked — use pterodactyl_power instead."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Game server host.",
                },
                "identifier": {
                    "type": "string",
                    "description": "Panel server ID or UUID.",
                },
                "command": {
                    "type": "string",
                    "description": "Console command to send (e.g. 'say Hello' for Minecraft).",
                },
                "game_type": {
                    "type": "string",
                    "description": (
                        "Game type if known: 'minecraft_java', 'minecraft_bedrock', "
                        "'rust', 'valheim', 'source', 'ark', 'terraria'. "
                        "Auto-detected from the server if omitted."
                    ),
                },
            },
            "required": ["server", "identifier", "command"],
        }

    async def execute(
        self,
        *,
        server: str,
        identifier: str,
        command: str,
        game_type: str = "",
        **kwargs: Any,
    ) -> ToolResult:
        """Send a console command via Client API with allowlist enforcement."""
        from agent.security.console_allowlist import (
            CommandAction,
            get_console_allowlist,
        )

        config = _get_panel_config(self._inventory, server)
        if not config:
            return ToolResult(error="Panel not configured.", exit_code=1)
        url, api_key = config

        # Auto-detect game type if not provided
        container_info = identifier
        if not game_type:
            try:
                server_data = _panel_client_get(url, api_key, f"servers/{identifier}")
                attrs = server_data.get("attributes", {})
                container_info = (
                    f"{attrs.get('name', '')} {attrs.get('docker_image', '')} "
                    f"{identifier}"
                )
            except RuntimeError:
                pass  # Detection is best-effort

        # Check command against game-aware allowlist
        allowlist = get_console_allowlist()
        check = allowlist.check_command(
            command,
            game_type=game_type or "unknown",
            container_info=container_info,
        )

        if check.action == CommandAction.DENY:
            return ToolResult(
                error=(
                    f"Command blocked: {check.reason}. "
                    f"Use pterodactyl_power to stop/restart servers."
                ),
                exit_code=1,
            )

        # For APPROVE actions, the registry's approval pipeline handles it
        # (pterodactyl_command is already in approval_required_patterns).
        # For ALLOW actions, we store a flag so the registry can skip approval.
        if check.action == CommandAction.ALLOW:
            # Mark this execution as pre-approved by the console allowlist
            kwargs["_console_preapproved"] = True

        try:
            _panel_post(url, api_key, f"servers/{identifier}/command", {"command": command})
        except RuntimeError as e:
            return ToolResult(error=str(e), exit_code=1)

        game_label = check.game_type if check.game_type != "unknown" else "game"
        return ToolResult(
            output=f"[{game_label}] Command sent to {identifier}: {command}"
        )
