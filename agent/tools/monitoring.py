"""VictoriaMetrics PromQL query tool.

Queries the VictoriaMetrics HTTP API for time series data.
Uses aiohttp or urllib — no external HTTP client dependency needed
since we can use the stdlib for simple GET requests.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any

import structlog

from agent.inventory import Inventory, ServerInfo
from agent.tools.base import BaseTool, ToolResult

logger = structlog.get_logger()

# Time range shorthand to seconds
_TIME_RANGES: dict[str, int] = {
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "3h": 10800,
    "6h": 21600,
    "12h": 43200,
    "24h": 86400,
    "2d": 172800,
    "7d": 604800,
}


class QueryMetrics(BaseTool):
    """Query VictoriaMetrics via PromQL."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "query_metrics"

    @property
    def description(self) -> str:
        return (
            "Query VictoriaMetrics using PromQL. Returns time series data "
            "for the specified query and time range. The monitoring server "
            "must have a metrics_url configured."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "query": {
                    "type": "string",
                    "description": "PromQL query string (e.g. 'up', 'node_cpu_seconds_total').",
                },
                "time_range": {
                    "type": "string",
                    "description": "Time range for the query (e.g. '1h', '24h', '7d'). Default '1h'.",
                    "default": "1h",
                },
                "step": {
                    "type": "string",
                    "description": "Query resolution step (e.g. '15s', '1m', '5m'). Default '1m'.",
                    "default": "1m",
                },
            },
            "required": ["query"],
        }

    async def execute(
        self,
        *,
        query: str,
        time_range: str = "1h",
        step: str = "1m",
        **kwargs: Any,
    ) -> ToolResult:
        """Execute a PromQL query against VictoriaMetrics."""
        # Find a server with a metrics_url
        metrics_server = self._find_metrics_server()
        if not metrics_server:
            return ToolResult(
                error="No server with metrics_url configured in inventory.",
                exit_code=1,
            )

        metrics_url = metrics_server.definition.metrics_url

        # Parse time range
        range_seconds = _TIME_RANGES.get(time_range)
        if range_seconds is None:
            return ToolResult(
                error=f"Unknown time range: {time_range!r}. "
                f"Use one of: {', '.join(_TIME_RANGES.keys())}",
                exit_code=1,
            )

        end_time = int(time.time())
        start_time = end_time - range_seconds

        # Build query URL
        params = urllib.parse.urlencode({
            "query": query,
            "start": start_time,
            "end": end_time,
            "step": step,
        })
        url = f"{metrics_url.rstrip('/')}/api/v1/query_range?{params}"

        logger.info("metrics_query", url=url, query=query, time_range=time_range)

        # Execute HTTP request
        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header("Accept", "application/json")

            # Add basic auth if configured
            auth = _resolve_metrics_auth(metrics_server.definition.metrics_auth)
            if auth:
                encoded = base64.b64encode(auth.encode("utf-8")).decode("ascii")
                req.add_header("Authorization", f"Basic {encoded}")

            import asyncio
            loop = asyncio.get_running_loop()
            response_data = await loop.run_in_executor(None, lambda: _fetch_url(req))

        except urllib.error.URLError as e:
            return ToolResult(error=f"Metrics query failed: {e}", exit_code=1)
        except TimeoutError:
            return ToolResult(error="Metrics query timed out", exit_code=1)

        # Parse and format response
        try:
            data = json.loads(response_data)
        except json.JSONDecodeError:
            return ToolResult(error="Invalid JSON response from metrics server", exit_code=1)

        if data.get("status") != "success":
            error_msg = data.get("error", "Unknown error")
            return ToolResult(error=f"Metrics query error: {error_msg}", exit_code=1)

        return ToolResult(
            output=_format_metrics_response(data),
            exit_code=0,
        )

    def _find_metrics_server(self) -> ServerInfo | None:
        """Find the first server with a metrics_url configured."""
        for name in self._inventory.server_names:
            server = self._inventory.get_server(name)
            if server.definition.metrics_url:
                return server
        return None


def _resolve_metrics_auth(auth_value: str | None) -> str | None:
    """Resolve metrics auth, reading from env var if prefixed with $."""
    if not auth_value:
        return None
    if auth_value.startswith("$"):
        return os.environ.get(auth_value[1:])
    return auth_value


def _fetch_url(req: urllib.request.Request, timeout: int = 10) -> str:
    """Fetch a URL and return the response body as a string."""
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def _format_metrics_response(data: dict) -> str:
    """Format a VictoriaMetrics response for display."""
    result = data.get("data", {})
    result_type = result.get("resultType", "unknown")
    results = result.get("result", [])

    if not results:
        return "No data returned for this query."

    lines: list[str] = [f"Result type: {result_type}", f"Series count: {len(results)}", ""]

    for series in results[:20]:  # Limit to 20 series for readability
        metric = series.get("metric", {})
        metric_str = ", ".join(f"{k}={v}" for k, v in metric.items())
        values = series.get("values", [])

        lines.append(f"--- {metric_str or '(no labels)'} ---")
        if values:
            # Show first and last few values
            if len(values) <= 6:
                for ts, val in values:
                    lines.append(f"  {_ts_to_str(ts)}: {val}")
            else:
                for ts, val in values[:3]:
                    lines.append(f"  {_ts_to_str(ts)}: {val}")
                lines.append(f"  ... ({len(values) - 6} more points)")
                for ts, val in values[-3:]:
                    lines.append(f"  {_ts_to_str(ts)}: {val}")
        lines.append("")

    if len(results) > 20:
        lines.append(f"... and {len(results) - 20} more series")

    return "\n".join(lines)


def _ts_to_str(ts: float) -> str:
    """Convert a Unix timestamp to a readable string."""
    import datetime
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
