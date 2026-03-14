"""Discord webhook alerting for health check results.

Sends alerts via Discord webhooks when ``bastion monitor`` detects
issues. Supports both rich embeds (colour-coded by severity) and
plain text fallback.

Configuration:
    Set DISCORD_WEBHOOK_URL environment variable or pass --discord-webhook
    to the monitor command.
"""

from __future__ import annotations

import json
import os
import socket
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import structlog

logger = structlog.get_logger()

# Discord embed colour codes
_COLOR_OK = 0x2ECC71       # Green
_COLOR_WARNING = 0xF39C12  # Orange
_COLOR_CRITICAL = 0xE74C3C # Red


def send_discord_alert(
    webhook_url: str,
    health_output: str,
    exit_code: int,
) -> bool:
    """Send a health check result to Discord via webhook.

    Args:
        webhook_url: Discord webhook URL.
        health_output: The health check output text.
        exit_code: 0 = all clear, 1 = issues found.

    Returns:
        True if the webhook was sent successfully.
    """
    hostname = socket.gethostname()

    if exit_code == 0:
        color = _COLOR_OK
        title = f"Health Check: All Clear"
        description = "All servers reporting healthy."
    else:
        # Count severity
        critical = health_output.count("✗")
        warnings = health_output.count("⚠")
        if critical > 0:
            color = _COLOR_CRITICAL
            title = f"Health Check: {critical} Critical, {warnings} Warning(s)"
        else:
            color = _COLOR_WARNING
            title = f"Health Check: {warnings} Warning(s)"
        description = _truncate_for_discord(health_output)

    payload: dict[str, Any] = {
        "embeds": [
            {
                "title": title,
                "description": description,
                "color": color,
                "footer": {"text": f"Bastion: {hostname}"},
            }
        ],
    }

    return _post_webhook(webhook_url, payload)


def _truncate_for_discord(text: str, max_len: int = 4000) -> str:
    """Truncate text to fit Discord embed description limit.

    Preserves issue lines (⚠ and ✗) and server headers (##).
    """
    # Extract only the important lines
    important: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if (
            stripped.startswith("##")
            or "⚠" in stripped
            or "✗" in stripped
            or "issue" in stripped.lower()
        ):
            important.append(stripped)

    result = "\n".join(important)
    if len(result) > max_len:
        result = result[:max_len - 20] + "\n... (truncated)"
    return result if result else text[:max_len]


def _post_webhook(url: str, payload: dict[str, Any]) -> bool:
    """POST JSON to a Discord webhook URL."""
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")

    try:
        with urlopen(req, timeout=10) as resp:
            return resp.status in (200, 204)
    except HTTPError as e:
        logger.error("discord_webhook_error", status=e.code, body=e.read().decode()[:200])
        return False
    except URLError as e:
        logger.error("discord_webhook_unreachable", reason=str(e.reason))
        return False
    except Exception as e:
        logger.error("discord_webhook_failed", error=str(e))
        return False
