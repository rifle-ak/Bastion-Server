"""CLI entry point for the bastion agent using Click."""

from __future__ import annotations

import asyncio
import os
import sys

import click
import structlog

from agent import __version__
from agent.config import load_all_config

logger = structlog.get_logger()


def _configure_logging(log_level: str) -> None:
    """Configure structlog for console output."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer() if sys.stderr.isatty() else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(structlog, log_level.upper(), structlog.INFO) if hasattr(structlog, log_level.upper()) else 20
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _asyncssh_available() -> bool:
    """Check if asyncssh can be imported without crashing.

    Uses a subprocess probe because asyncssh's cryptography backend
    can cause an unrecoverable Rust panic if the C bindings are broken.
    """
    import subprocess
    result = subprocess.run(
        [sys.executable, "-c", "import asyncssh"],
        capture_output=True,
        timeout=5,
    )
    return result.returncode == 0


def _build_agent(config_path: str):
    """Build all agent components from config.

    Returns:
        Tuple of (ConversationClient, AuditLogger).
    """
    from agent.client import ConversationClient
    from agent.inventory import Inventory
    from agent.prompts import build_system_prompt
    from agent.security.audit import AuditLogger
    from agent.tools.docker_tools import DockerLogs, DockerPs
    from agent.tools.files import ReadFile
    from agent.tools.local import RunLocalCommand
    from agent.tools.monitoring import QueryMetrics
    from agent.tools.registry import ToolRegistry
    from agent.tools.server_info import GetServerStatus, ListServers
    from agent.tools.systemd import ServiceJournal, ServiceStatus
    from agent.ui.terminal import TerminalUI

    agent_cfg, servers_cfg, permissions_cfg = load_all_config(config_path)
    inventory = Inventory(servers_cfg, permissions_cfg)
    audit = AuditLogger(agent_cfg.audit_log_path)

    # Build tool registry and register all tools
    registry = ToolRegistry(agent_cfg, inventory, audit)
    registry.register(RunLocalCommand())
    registry.register(ReadFile(inventory))
    registry.register(ListServers(inventory))
    registry.register(GetServerStatus(inventory))
    registry.register(DockerPs(inventory))
    registry.register(DockerLogs(inventory))
    registry.register(ServiceStatus(inventory))
    registry.register(ServiceJournal(inventory))
    registry.register(QueryMetrics(inventory))

    # Register SSH tools if asyncssh is available
    if _asyncssh_available():
        from agent.tools.remote import RunRemoteCommand
        registry.register(RunRemoteCommand(inventory, agent_cfg.command_timeout))
    else:
        logger.warning("ssh_tools_unavailable", msg="SSH tools disabled (asyncssh not available)")

    # Build system prompt and UI
    system_prompt = build_system_prompt(inventory, registry)
    ui = TerminalUI()

    # Show banner
    ui.display_banner(__version__, agent_cfg.model, list(servers_cfg.servers.keys()))

    # Build conversation client
    client = ConversationClient(agent_cfg, registry, system_prompt, ui)

    return client, audit


@click.group()
@click.version_option(version=__version__, prog_name="bastion-agent")
def cli() -> None:
    """Bastion Agent - Infrastructure management for Galaxy Gaming Host."""


@cli.command()
@click.option(
    "--config-dir",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    default=None,
    help="Path to configuration directory. Defaults to BASTION_AGENT_CONFIG env or ./config/",
)
@click.option(
    "--log-level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    default=None,
    help="Logging level. Defaults to BASTION_AGENT_LOG_LEVEL env or INFO.",
)
def run(config_dir: str | None, log_level: str | None) -> None:
    """Start the interactive bastion agent session."""
    config_path = config_dir or os.environ.get("BASTION_AGENT_CONFIG", "./config")
    level = log_level or os.environ.get("BASTION_AGENT_LOG_LEVEL", "INFO")

    _configure_logging(level)

    # Check for API key early
    if not os.environ.get("ANTHROPIC_API_KEY"):
        click.echo("Error: ANTHROPIC_API_KEY environment variable is not set.", err=True)
        sys.exit(1)

    try:
        client, audit = _build_agent(config_path)
    except FileNotFoundError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Startup error: {e}", err=True)
        logger.exception("startup_failed")
        sys.exit(1)

    audit.log_session_start()
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        click.echo("\nSession interrupted.")
    finally:
        audit.log_session_end()
        audit.close()
        logger.info("session_ended")


@cli.command()
@click.option(
    "--config-dir",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    default=None,
    help="Path to configuration directory.",
)
def check_config(config_dir: str | None) -> None:
    """Validate configuration files without starting the agent."""
    config_path = config_dir or os.environ.get("BASTION_AGENT_CONFIG", "./config")

    try:
        agent_cfg, servers_cfg, permissions_cfg = load_all_config(config_path)
    except FileNotFoundError as e:
        click.echo(f"FAIL: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"FAIL: {e}", err=True)
        sys.exit(1)

    click.echo("Configuration OK")
    click.echo(f"  Agent model: {agent_cfg.model}")
    click.echo(f"  Approval mode: {agent_cfg.approval_mode.value}")
    click.echo(f"  Servers: {len(servers_cfg.servers)}")
    for name, server in servers_cfg.servers.items():
        click.echo(f"    - {name} ({server.role}): {server.host}")
    click.echo(f"  Roles with permissions: {', '.join(permissions_cfg.roles.keys())}")
    click.echo(f"  Approval patterns: {len(permissions_cfg.approval_required_patterns)}")


if __name__ == "__main__":
    cli()
