"""CLI entry point for the bastion agent using Click."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click
import structlog

from agent import __version__
from agent.config import load_all_config

logger = structlog.get_logger()


def _configure_logging(log_level: str) -> None:
    """Configure structlog for JSON output."""
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

    try:
        agent_cfg, servers_cfg, permissions_cfg = load_all_config(config_path)
    except FileNotFoundError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Configuration error: {e}", err=True)
        sys.exit(1)

    logger.info(
        "config_loaded",
        config_dir=config_path,
        model=agent_cfg.model,
        servers=list(servers_cfg.servers.keys()),
    )

    click.echo(f"Bastion Agent v{__version__}")
    click.echo(f"Model: {agent_cfg.model}")
    click.echo(f"Servers: {', '.join(servers_cfg.servers.keys())}")
    click.echo("Agent session not yet implemented — coming in build step 5.")


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
