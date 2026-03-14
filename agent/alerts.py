"""Multi-channel alerting for health check results.

Supports Discord webhooks, Slack webhooks, and email (SMTP).
Sends alerts when ``bastion monitor`` detects issues.

Configuration via environment variables:
    DISCORD_WEBHOOK_URL  — Discord webhook URL
    SLACK_WEBHOOK_URL    — Slack webhook URL
    ALERT_EMAIL_TO       — Email recipient(s), comma-separated
    ALERT_EMAIL_FROM     — Sender address (default: bastion@hostname)
    ALERT_SMTP_HOST      — SMTP server (default: localhost)
    ALERT_SMTP_PORT      — SMTP port (default: 25)
"""

from __future__ import annotations

import json
import os
import smtplib
import socket
from email.mime.text import MIMEText
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import structlog

logger = structlog.get_logger()

# Discord embed colour codes
_COLOR_OK = 0x2ECC71       # Green
_COLOR_WARNING = 0xF39C12  # Orange
_COLOR_CRITICAL = 0xE74C3C # Red


def send_all_alerts(
    health_output: str,
    exit_code: int,
    discord_url: str | None = None,
    slack_url: str | None = None,
    email_to: str | None = None,
) -> dict[str, bool]:
    """Send alerts to all configured channels.

    Returns dict of channel -> success.
    """
    results: dict[str, bool] = {}

    discord = discord_url or os.environ.get("DISCORD_WEBHOOK_URL", "")
    if discord:
        results["discord"] = send_discord_alert(discord, health_output, exit_code)

    slack = slack_url or os.environ.get("SLACK_WEBHOOK_URL", "")
    if slack:
        results["slack"] = send_slack_alert(slack, health_output, exit_code)

    email = email_to or os.environ.get("ALERT_EMAIL_TO", "")
    if email:
        results["email"] = send_email_alert(email, health_output, exit_code)

    return results


def send_discord_alert(
    webhook_url: str,
    health_output: str,
    exit_code: int,
) -> bool:
    """Send a health check result to Discord via webhook."""
    hostname = socket.gethostname()

    if exit_code == 0:
        color = _COLOR_OK
        title = "Health Check: All Clear"
        description = "All servers reporting healthy."
    else:
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


def send_slack_alert(
    webhook_url: str,
    health_output: str,
    exit_code: int,
) -> bool:
    """Send a health check result to Slack via incoming webhook."""
    hostname = socket.gethostname()

    if exit_code == 0:
        emoji = ":white_check_mark:"
        title = "Health Check: All Clear"
        color = "good"
    else:
        critical = health_output.count("✗")
        warnings = health_output.count("⚠")
        if critical > 0:
            emoji = ":x:"
            title = f"Health Check: {critical} Critical, {warnings} Warning(s)"
            color = "danger"
        else:
            emoji = ":warning:"
            title = f"Health Check: {warnings} Warning(s)"
            color = "warning"

    text = _truncate_for_discord(health_output, max_len=3000)

    payload: dict[str, Any] = {
        "attachments": [
            {
                "fallback": title,
                "color": color,
                "title": f"{emoji} {title}",
                "text": text,
                "footer": f"Bastion: {hostname}",
                "mrkdwn_in": ["text"],
            }
        ],
    }

    return _post_webhook(webhook_url, payload)


def send_email_alert(
    recipients: str,
    health_output: str,
    exit_code: int,
) -> bool:
    """Send a health check result via email (SMTP)."""
    hostname = socket.gethostname()
    from_addr = os.environ.get("ALERT_EMAIL_FROM", f"bastion@{hostname}")
    smtp_host = os.environ.get("ALERT_SMTP_HOST", "localhost")
    smtp_port = int(os.environ.get("ALERT_SMTP_PORT", "25"))

    if exit_code == 0:
        subject = f"[Bastion] Health Check OK — {hostname}"
        body = "All servers reporting healthy.\n\n" + health_output
    else:
        critical = health_output.count("✗")
        warnings = health_output.count("⚠")
        subject = f"[Bastion] ALERT: {critical} Critical, {warnings} Warning(s) — {hostname}"
        body = health_output

    to_list = [r.strip() for r in recipients.split(",") if r.strip()]

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_list)

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as smtp:
            smtp.sendmail(from_addr, to_list, msg.as_string())
        return True
    except Exception as e:
        logger.error("email_alert_failed", error=str(e))
        return False


def _truncate_for_discord(text: str, max_len: int = 4000) -> str:
    """Truncate text to fit embed description limits.

    Preserves issue lines (⚠ and ✗) and server headers (##).
    """
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
    """POST JSON to a webhook URL."""
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")

    try:
        with urlopen(req, timeout=10) as resp:
            return resp.status in (200, 204)
    except HTTPError as e:
        logger.error("webhook_error", url=url[:50], status=e.code)
        return False
    except URLError as e:
        logger.error("webhook_unreachable", url=url[:50], reason=str(e.reason))
        return False
    except Exception as e:
        logger.error("webhook_failed", url=url[:50], error=str(e))
        return False
