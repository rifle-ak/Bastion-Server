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


def _build_core(config_path: str):
    """Build the core agent components (config, inventory, registry, prompt).

    Returns:
        Tuple of (AgentConfig, ServersConfig, Inventory, ToolRegistry,
        system_prompt_str, AuditLogger).
    """
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

    system_prompt = build_system_prompt(inventory, registry)

    return agent_cfg, servers_cfg, inventory, registry, system_prompt, audit


def _build_agent(config_path: str):
    """Build all agent components for interactive mode.

    Returns:
        Tuple of (ConversationClient, AuditLogger).
    """
    from agent.client import ConversationClient
    from agent.ui.terminal import TerminalUI

    agent_cfg, servers_cfg, _inv, registry, system_prompt, audit = _build_core(config_path)

    ui = TerminalUI()
    ui.display_banner(__version__, agent_cfg.model, list(servers_cfg.servers.keys()))
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


@cli.command()
@click.option(
    "--config-dir",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    default=None,
    help="Path to configuration directory.",
)
@click.option(
    "--log-level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    default=None,
    help="Logging level.",
)
@click.option(
    "--socket",
    "socket_path",
    type=click.Path(),
    default=None,
    help="Unix socket path. Defaults to config value or /run/bastion-agent/agent.sock.",
)
def daemon(config_dir: str | None, log_level: str | None, socket_path: str | None) -> None:
    """Start the agent as a persistent daemon listening on a Unix socket.

    In daemon mode the agent waits for client connections on a Unix
    domain socket instead of reading from stdin.  Each connected client
    gets an independent conversation session.  Destructive operations
    are auto-denied (no interactive terminal for approval prompts).

    Use ``bastion-agent send`` to talk to the running daemon.
    """
    config_path = config_dir or os.environ.get("BASTION_AGENT_CONFIG", "./config")
    level = log_level or os.environ.get("BASTION_AGENT_LOG_LEVEL", "INFO")

    _configure_logging(level)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        click.echo("Error: ANTHROPIC_API_KEY environment variable is not set.", err=True)
        sys.exit(1)

    try:
        agent_cfg, servers_cfg, _inv, registry, system_prompt, audit = _build_core(config_path)
    except FileNotFoundError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Startup error: {e}", err=True)
        logger.exception("startup_failed")
        sys.exit(1)

    # Force auto_deny in daemon mode — no terminal for approval prompts
    agent_cfg = agent_cfg.model_copy(update={"approval_mode": "auto_deny"})

    sock = socket_path or agent_cfg.socket_path
    asyncio.run(_run_daemon(agent_cfg, registry, system_prompt, audit, servers_cfg, sock))


async def _run_daemon(agent_cfg, registry, system_prompt, audit, servers_cfg, socket_path: str):
    """Async entry point for daemon mode."""
    import signal

    from agent.client import CancelledByUser, ConversationClient
    from agent.sessions import SessionStore
    from agent.ui.daemon import DaemonUI

    ui = DaemonUI(socket_path)
    await ui.start()
    client = ConversationClient(agent_cfg, registry, system_prompt, ui)
    client.set_cancel_event(ui.cancelled_event)
    store = SessionStore(agent_cfg.sessions_dir)

    server_names = list(servers_cfg.servers.keys())
    logger.info(
        "daemon_started",
        model=agent_cfg.model,
        servers=server_names,
        socket=socket_path,
    )

    # Graceful shutdown on SIGTERM / SIGINT
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(ui.stop()))

    async def _process_with_cancel(message: str) -> bool:
        """Process a message with cancellation support.

        Returns True if completed normally, False if cancelled.
        """
        ui.start_processing()
        try:
            await client.process_message(message)
            return True
        except CancelledByUser:
            logger.info("operation_cancelled_by_user")
            ui.display_cancelled()
            return False
        finally:
            ui.stop_processing()

    # Session loop — one iteration per client connection
    while True:
        got_client = await ui.wait_for_client()
        if not got_client:
            break  # Shutting down

        audit.log_session_start()
        ui.display_banner(__version__, agent_cfg.model, server_names)

        session_id = store.create_id()
        created_at: float | None = None

        try:
            # Check if the first message requests a session resume
            first_message = await ui.get_input()
            if first_message is None:
                continue  # Client disconnected immediately

            meta = ui.last_metadata
            resume_id = meta.get("resume")
            if resume_id:
                try:
                    messages, created_at = store.load(resume_id)
                    client.restore_messages(messages)
                    session_id = resume_id
                    ui.display_info(f"Resumed session {session_id} ({len(messages)} messages)")
                    logger.info("session_resumed", session_id=session_id)
                except FileNotFoundError:
                    ui.display_error(f"Session {resume_id} not found")

            # Process the first message (even on resume, the client sends a real message)
            if first_message and first_message not in ("/quit", "/exit"):
                await _process_with_cancel(first_message)
                store.save(session_id, client.get_messages(), created_at=created_at)
                ui.display_done()
                await ui.flush()
            elif first_message in ("/quit", "/exit"):
                ui.display_goodbye()
                continue

            # Continue processing subsequent messages
            while True:
                message = await ui.get_input()
                if message is None:
                    break  # Client disconnected
                if message in ("/quit", "/exit"):
                    ui.display_goodbye()
                    break
                if not message:
                    continue
                await _process_with_cancel(message)
                store.save(session_id, client.get_messages(), created_at=created_at)
                ui.display_done()
                await ui.flush()
        except Exception:
            logger.exception("session_error")
        finally:
            # Save final state before cleanup
            if client.get_messages():
                store.save(session_id, client.get_messages(), created_at=created_at)
                ui.display_info(f"Session saved: {session_id}")
            ui.display_goodbye()
            await ui.flush()
            client.reset()
            audit.log_session_end()

    await ui.stop()
    audit.close()
    logger.info("daemon_exited")


@cli.command()
@click.argument("message", required=False)
@click.option(
    "--socket",
    "socket_path",
    type=click.Path(),
    default="/run/bastion-agent/agent.sock",
    help="Unix socket path of the running daemon.",
)
@click.option(
    "--interactive", "-i",
    is_flag=True,
    default=False,
    help="Stay connected for a multi-turn conversation.",
)
@click.option(
    "--verbose", "-v",
    is_flag=True,
    default=False,
    help="Show full tool call details and results (default: compact).",
)
@click.option(
    "--resume", "-r",
    "resume_id",
    type=str,
    default=None,
    help="Resume a previous session by ID.",
)
@click.option(
    "--sessions",
    "list_sessions",
    is_flag=True,
    default=False,
    help="List saved sessions and exit.",
)
@click.option(
    "--sessions-dir",
    type=click.Path(),
    default="./sessions",
    help="Directory for saved sessions (only used with --sessions).",
)
def send(
    message: str | None,
    socket_path: str,
    interactive: bool,
    verbose: bool,
    resume_id: str | None,
    list_sessions: bool,
    sessions_dir: str,
) -> None:
    """Send a message to the running daemon and display the response.

    Examples:

      bastion-agent send "check disk space on gameserver-01"

      bastion-agent send -v "check disk space"  # verbose output

      bastion-agent send -i   # interactive session

      bastion-agent send --sessions  # list saved sessions

      bastion-agent send -r abc123def456 "follow up question"  # resume
    """
    if list_sessions:
        _show_sessions(sessions_dir)
        return

    if not message and not interactive:
        click.echo("Error: provide a message or use --interactive / -i", err=True)
        sys.exit(1)

    try:
        asyncio.run(_send_message(socket_path, message, interactive, verbose, resume_id))
    except FileNotFoundError:
        click.echo(f"Error: socket not found at {socket_path}. Is the daemon running?", err=True)
        sys.exit(1)
    except ConnectionRefusedError:
        click.echo("Error: connection refused. Is the daemon running?", err=True)
        sys.exit(1)
    except KeyboardInterrupt:
        click.echo("\nDisconnected.")


def _show_sessions(sessions_dir: str) -> None:
    """Display saved sessions in a table."""
    from datetime import datetime

    from agent.sessions import SessionStore

    store = SessionStore(sessions_dir)
    sessions = store.list_sessions()
    if not sessions:
        click.echo("No saved sessions.")
        return

    click.echo(f"{'ID':<14} {'Updated':<20} {'Turns':<6} {'Preview'}")
    click.echo("-" * 80)
    for s in sessions:
        updated = datetime.fromtimestamp(s.updated_at).strftime("%Y-%m-%d %H:%M")
        click.echo(f"{s.session_id:<14} {updated:<20} {s.turns:<6} {s.preview}")


async def _send_message(
    socket_path: str,
    message: str | None,
    interactive: bool,
    verbose: bool = False,
    resume_id: str | None = None,
) -> None:
    """Connect to the daemon and exchange messages."""
    import json as _json
    import signal

    reader, writer = await asyncio.open_unix_connection(socket_path)
    _cancel_sent = False

    async def _send_cancel() -> None:
        """Send a cancel signal to the daemon."""
        nonlocal _cancel_sent
        if _cancel_sent:
            return
        _cancel_sent = True
        try:
            cancel_msg = _json.dumps({"type": "cancel"}) + "\n"
            writer.write(cancel_msg.encode())
            await writer.drain()
        except (ConnectionError, OSError):
            pass

    async def _read_events() -> None:
        """Read and display events until a 'done', 'cancelled', or 'goodbye' event."""
        while True:
            line = await reader.readline()
            if not line:
                return
            try:
                event = _json.loads(line.decode().strip())
            except _json.JSONDecodeError:
                continue

            etype = event.get("type", "")
            if etype == "response":
                click.echo(event.get("text", ""))
            elif etype == "tool_call":
                tool = event.get("tool", "?")
                if verbose:
                    inp = event.get("input", {})
                    parts = [f"{k}={v}" for k, v in inp.items() if isinstance(v, (str, int, bool))]
                    click.echo(f"  > {tool}  {' '.join(parts)}", err=True)
                else:
                    click.echo(f"  [{tool}]", err=True)
            elif etype == "tool_result":
                if verbose:
                    result = event.get("result", {})
                    out = result.get("output", "")
                    err = result.get("error", "")
                    if out:
                        click.echo(out, err=True)
                    if err and not out:
                        click.echo(f"  error: {err}", err=True)
                else:
                    # Compact: only show errors, suppress normal output
                    result = event.get("result", {})
                    err = result.get("error", "")
                    if err:
                        click.echo(f"  error: {err}", err=True)
            elif etype == "cancelled":
                click.echo("Cancelled.", err=True)
                return
            elif etype == "error":
                click.echo(f"Error: {event.get('text', '')}", err=True)
            elif etype in ("goodbye", "done"):
                return
            elif etype == "banner":
                continue  # Suppress banner in send mode
            elif etype == "info":
                click.echo(event.get("text", ""), err=True)

    async def _send(msg: str, extra: dict | None = None) -> None:
        payload_dict = {"message": msg}
        if extra:
            payload_dict.update(extra)
        payload = _json.dumps(payload_dict) + "\n"
        writer.write(payload.encode())
        await writer.drain()

    # Register Ctrl-C handler to send cancel before disconnecting
    loop = asyncio.get_running_loop()
    _interrupted = False

    def _on_sigint() -> None:
        nonlocal _interrupted
        if _interrupted:
            # Second Ctrl-C — force exit
            writer.close()
            return
        _interrupted = True
        click.echo("\nCancelling... (press Ctrl-C again to force quit)", err=True)
        asyncio.ensure_future(_send_cancel())

    try:
        loop.add_signal_handler(signal.SIGINT, _on_sigint)
    except NotImplementedError:
        pass  # Windows fallback — Ctrl-C will just raise KeyboardInterrupt

    try:
        if message:
            extra = {"resume": resume_id} if resume_id else None
            await _send(message, extra=extra)
            await _read_events()

        if interactive and not _interrupted:
            while True:
                try:
                    raw = await loop.run_in_executor(None, lambda: input("[bastion] > "))
                    text = raw.strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not text:
                    continue
                if text in ("/quit", "/exit"):
                    await _send(text)
                    await _read_events()
                    break
                _interrupted = False
                _cancel_sent = False
                await _send(text)
                await _read_events()
    finally:
        try:
            loop.remove_signal_handler(signal.SIGINT)
        except (NotImplementedError, ValueError):
            pass
        writer.close()


if __name__ == "__main__":
    cli()
