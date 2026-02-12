# Bastion Agent - Claude Code Instructions

## Project Overview

This is **bastion-agent**, a Python-based infrastructure management agent powered by the Anthropic Claude API with tool use. It runs on a hardened bastion server and provides SSH-based access to downstream servers in a hosting infrastructure (Galaxy Gaming Host).

The agent acts as an intelligent assistant that can execute commands locally and on remote servers, query monitoring systems, inspect Docker containers, read logs, and perform administrative tasks — all with structured audit logging, command allowlisting, and a human approval gate for destructive operations.

**This is production infrastructure tooling. Security is not optional.**

## Tech Stack

- **Python 3.12+** (system Python on Ubuntu 24.04)
- **Anthropic Python SDK** (`anthropic`) — for Claude API with tool use
- **asyncio + asyncssh** — for non-blocking SSH to downstream hosts
- **PyYAML** — for configuration files
- **Rich** — for terminal UI (conversation interface)
- **structlog** — for structured JSON audit logging
- **Click** — for CLI entry point
- **pytest + pytest-asyncio** — for testing

Do NOT use LangChain, LlamaIndex, CrewAI, or any other "agent framework". This is a clean, direct integration with the Anthropic API using native tool use. No unnecessary abstraction layers.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   BASTION SERVER                     │
│                                                      │
│  ┌──────────────────────────────────────────────┐   │
│  │              bastion-agent                     │   │
│  │                                                │   │
│  │  User ↔ CLI Interface (Rich)                  │   │
│  │           ↕                                    │   │
│  │  Conversation Manager                          │   │
│  │           ↕                                    │   │
│  │  Anthropic API Client (tool use)              │   │
│  │           ↕                                    │   │
│  │  Tool Router                                   │   │
│  │     ↕           ↕           ↕                  │   │
│  │  Security    Tool Impls   Audit Logger         │   │
│  │  (allowlist, (local, ssh, (structured JSON)    │   │
│  │   approval)   docker,                          │   │
│  │              metrics,                          │   │
│  │              files)                            │   │
│  └──────────────────────────────────────────────┘   │
│                    │ SSH (per-host keys)              │
│                    ↓                                  │
│  ┌─────────┐ ┌─────────┐ ┌─────────────┐           │
│  │ Game    │ │ Monitor │ │ Other       │           │
│  │ Servers │ │ Stack   │ │ Downstream  │           │
│  └─────────┘ └─────────┘ └─────────────┘           │
└─────────────────────────────────────────────────────┘
```

## Project Structure

```
bastion-agent/
├── CLAUDE.md                    # This file
├── README.md                    # User-facing documentation
├── install.sh                   # One-command production installer
├── pyproject.toml               # Project metadata & dependencies (use this, not setup.py)
├── requirements.txt             # Pinned dependencies for reproducibility
├── agent/
│   ├── __init__.py              # Package init, version
│   ├── main.py                  # Entry point, Click CLI
│   ├── client.py                # Anthropic API wrapper, conversation loop
│   ├── config.py                # Pydantic settings, load YAML configs
│   ├── inventory.py             # Server inventory model & loader
│   ├── prompts.py               # System prompt builder (assembles from inventory + config)
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── registry.py          # Tool registration, schema generation, dispatch
│   │   ├── base.py              # Base tool class / protocol
│   │   ├── local.py             # Local command execution (bastion itself)
│   │   ├── remote.py            # SSH command execution on downstream hosts
│   │   ├── files.py             # Scoped file read operations
│   │   ├── server_info.py       # list_servers, get_server_status
│   │   ├── docker_tools.py      # docker_ps, docker_logs (local + remote)
│   │   ├── systemd.py           # service_status, service_journal
│   │   └── monitoring.py        # VictoriaMetrics PromQL queries
│   ├── security/
│   │   ├── __init__.py
│   │   ├── allowlist.py         # Command allowlist engine (pattern-based)
│   │   ├── approval.py          # Human-in-the-loop approval for destructive ops
│   │   ├── audit.py             # Structured JSON audit log with structlog
│   │   └── sanitizer.py         # Input sanitization, shell injection prevention
│   └── ui/
│       ├── __init__.py
│       └── terminal.py          # Rich-based terminal interface
├── config/
│   ├── agent.yaml               # Agent behavior configuration
│   ├── servers.yaml             # Server inventory definition
│   └── permissions.yaml         # Per-role and per-server allowed operations
├── scripts/
│   ├── setup-bastion.sh         # Bastion server hardening script
│   ├── setup-downstream.sh      # Downstream server prep script (run per-host)
│   └── generate-ssh-keys.sh     # Generate per-host SSH keypairs
├── systemd/
│   └── bastion-agent.service    # Systemd unit file
├── tests/
│   ├── conftest.py
│   ├── test_sanitizer.py
│   ├── test_allowlist.py
│   ├── test_approval.py
│   ├── test_audit.py
│   ├── test_inventory.py
│   └── test_tools.py
└── logs/                        # Audit logs directory (gitignored)
```

## Critical Design Decisions

### 1. Tool Execution Pipeline

EVERY tool call from Claude goes through this exact pipeline. No exceptions:

```python
async def execute_tool(tool_name: str, tool_input: dict) -> dict:
    # 1. Sanitize inputs (shell injection prevention)
    sanitized = sanitizer.sanitize(tool_name, tool_input)

    # 2. Log the attempt (before execution, always)
    audit.log_attempt(tool_name, sanitized)

    # 3. Check allowlist (is this operation permitted at all?)
    if not allowlist.is_permitted(tool_name, sanitized):
        audit.log_denied(tool_name, sanitized, reason="allowlist")
        return {"error": f"Operation not permitted by security policy"}

    # 4. Check if human approval is required
    if approval.requires_confirmation(tool_name, sanitized):
        approved = await approval.request_human_approval(tool_name, sanitized)
        if not approved:
            audit.log_denied(tool_name, sanitized, reason="human_denied")
            return {"error": "Operation denied by operator"}

    # 5. Execute with timeout
    try:
        result = await tools.dispatch(tool_name, sanitized, timeout=30)
    except TimeoutError:
        audit.log_timeout(tool_name, sanitized)
        return {"error": "Operation timed out (30s)"}
    except Exception as e:
        audit.log_error(tool_name, sanitized, error=str(e))
        return {"error": f"Execution failed: {str(e)}"}

    # 6. Log the result
    audit.log_success(tool_name, sanitized, result=result)
    return result
```

### 2. Security Model

**Allowlisting, not blocklisting.** We define what IS allowed, not what isn't.

- Each server role has a set of permitted operations defined in `permissions.yaml`
- Commands are validated against patterns BEFORE execution
- Shell metacharacters (`;`, `|`, `&`, `$()`, backticks) in command arguments are REJECTED, not escaped
- File operations are scoped to specific directories per server
- SSH connections use per-host dedicated keypairs with forced commands where possible

**Destructive operation classification:**

Operations requiring human approval:
- Any command containing: `restart`, `stop`, `kill`, `rm`, `delete`, `drop`, `truncate`, `write`, `mv`, `cp` (to system paths)
- Docker: `restart`, `stop`, `rm`, `down`, `prune`
- Systemd: `restart`, `stop`, `enable`, `disable`
- File writes of any kind
- Any `sudo` command

Operations that execute freely (read-only):
- `status`, `ps`, `ls`, `cat`, `head`, `tail`, `grep`, `df`, `free`, `top`, `uptime`
- Docker: `ps`, `logs`, `inspect`, `stats`
- Systemd: `status`, `is-active`, `list-units`
- Journalctl reads
- Metric queries
- File reads (within scoped paths)

### 3. Input Sanitization

The sanitizer MUST prevent shell injection. This is the approach:

```python
# REJECT these characters in command arguments entirely
# Do NOT try to escape them — reject the input
FORBIDDEN_PATTERNS = [
    r'[;&|]',           # Command chaining
    r'\$\(',            # Command substitution
    r'`',               # Backtick substitution
    r'\.\.',            # Path traversal
    r'>\s*/',           # Redirect to absolute path
    r'>>\s*/',          # Append to absolute path
    r'\b(eval|exec)\b', # Code execution
]
```

For commands that need pipes or chaining (like `grep`), build them programmatically in the tool implementation — never pass raw shell strings from the model.

### 4. SSH Execution

Use `asyncssh` for all remote operations. Never shell out to `ssh` directly.

```python
async def run_remote(server_name: str, command: str, timeout: int = 30) -> CommandResult:
    server = inventory.get_server(server_name)
    async with asyncssh.connect(
        server.host,
        username=server.user,
        client_keys=[server.key_path],
        known_hosts=server.known_hosts_path,
    ) as conn:
        result = await asyncio.wait_for(
            conn.run(command, check=False),
            timeout=timeout
        )
        return CommandResult(
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_status,
            server=server_name,
        )
```

### 5. Anthropic API Integration

Use the Anthropic Python SDK directly. Model: `claude-sonnet-4-5-20250514` (good balance of speed and capability for operational tasks — switch to opus for complex analysis if needed).

The conversation loop:

```python
async def conversation_loop(self):
    messages = []

    while True:
        user_input = await self.ui.get_input()
        if user_input in ("/quit", "/exit"):
            break

        messages.append({"role": "user", "content": user_input})

        # Loop until Claude stops requesting tool calls
        while True:
            response = self.client.messages.create(
                model=self.config.model,
                max_tokens=4096,
                system=self.system_prompt,
                tools=self.tool_schemas,
                messages=messages,
            )

            # Collect all content blocks
            assistant_content = response.content
            messages.append({"role": "assistant", "content": assistant_content})

            # If no tool use, we're done — display text response
            if response.stop_reason == "end_turn":
                for block in assistant_content:
                    if block.type == "text":
                        self.ui.display_response(block.text)
                break

            # Process tool calls
            tool_results = []
            for block in assistant_content:
                if block.type == "tool_use":
                    self.ui.display_tool_call(block.name, block.input)
                    result = await self.execute_tool(block.name, block.input)
                    self.ui.display_tool_result(block.name, result)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    })

            messages.append({"role": "user", "content": tool_results})
```

### 6. Configuration Schema

**`config/agent.yaml`:**
```yaml
model: claude-sonnet-4-5-20250514
max_tokens: 4096
max_tool_iterations: 10        # Safety: max tool call rounds per user message
command_timeout: 30            # Default timeout in seconds
audit_log_path: ./logs/audit.jsonl
approval_mode: interactive     # "interactive" (terminal prompt) or "auto_deny"
```

**`config/servers.yaml`:**
```yaml
servers:
  localhost:
    host: localhost
    role: bastion
    user: claude-agent
    description: "Bastion server (this machine)"
    ssh: false  # Local execution, no SSH needed

  gameserver-01:
    host: 10.0.1.10
    role: game-server
    user: claude-agent
    key_path: ~/.ssh/keys/gameserver-01_ed25519
    description: "Primary Rust game server - Pterodactyl Wings node"
    services:
      - pterodactyl-wings
      - docker

  monitoring:
    host: 10.0.1.20
    role: monitoring
    user: claude-agent
    key_path: ~/.ssh/keys/monitoring_ed25519
    description: "VictoriaMetrics + Grafana monitoring stack"
    services:
      - victoriametrics
      - grafana
      - vmagent
    metrics_url: http://10.0.1.20:8428  # VictoriaMetrics API endpoint
```

**`config/permissions.yaml`:**
```yaml
roles:
  bastion:
    allowed_commands:
      - "uptime"
      - "df -h"
      - "free -h"
      - "ps aux"
      - "systemctl status *"
      - "journalctl -u * --no-pager -n *"
      - "docker ps"
      - "docker logs *"
      - "cat /var/log/*"
      - "tail -n * /var/log/*"
    allowed_paths_read:
      - /var/log/
      - /etc/
      - /home/claude-agent/
    allowed_paths_write: []

  game-server:
    allowed_commands:
      - "uptime"
      - "df -h"
      - "free -h"
      - "ps aux"
      - "systemctl status *"
      - "journalctl -u * --no-pager -n *"
      - "docker ps *"
      - "docker logs *"
      - "docker inspect *"
      - "docker stats --no-stream"
      - "docker restart *"          # REQUIRES APPROVAL
      - "systemctl restart *"       # REQUIRES APPROVAL
    allowed_paths_read:
      - /var/log/
      - /etc/pterodactyl/
      - /srv/pterodactyl/
    allowed_paths_write: []

  monitoring:
    allowed_commands:
      - "uptime"
      - "df -h"
      - "free -h"
      - "docker ps *"
      - "docker logs *"
      - "docker compose -f /opt/monitoring/* ps"
      - "docker compose -f /opt/monitoring/* logs *"
      - "docker compose -f /opt/monitoring/* restart *"  # REQUIRES APPROVAL
      - "systemctl status *"
      - "journalctl -u * --no-pager -n *"
    allowed_paths_read:
      - /var/log/
      - /opt/monitoring/
    allowed_paths_write: []

# Patterns that ALWAYS require human approval regardless of role
approval_required_patterns:
  - "restart"
  - "stop"
  - "kill"
  - "rm "
  - "remove"
  - "delete"
  - "drop"
  - "truncate"
  - "write"
  - "tee "
  - ">"
  - ">>"
```

## Tool Definitions

Each tool must be implemented as a class with:
- A `name` property (string)
- A `description` property (string, clear and specific — Claude reads this)
- A `parameters` property (JSON Schema dict)
- An async `execute(self, **kwargs) -> dict` method

### Required Tools

**1. `run_local_command`**
- Execute a command on the bastion server itself
- Input: `command` (string)
- Validates against bastion allowlist
- Uses `asyncio.create_subprocess_exec` (NOT `shell=True`)

**2. `run_remote_command`**
- Execute a command on a downstream server via SSH
- Input: `server` (string, must exist in inventory), `command` (string)
- Validates against that server's role allowlist
- Uses asyncssh

**3. `query_metrics`**
- Query VictoriaMetrics via HTTP API
- Input: `query` (PromQL string), `time_range` (optional, e.g. "1h", "24h"), `step` (optional)
- Hits `{metrics_url}/api/v1/query_range`

**4. `list_servers`**
- Return the server inventory with roles and descriptions
- No input required
- Read-only, always permitted

**5. `get_server_status`**
- Quick health check: uptime, load, disk, memory for a server
- Input: `server` (string)
- Runs multiple commands and aggregates

**6. `read_file`**
- Read a file's contents (with line limit)
- Input: `server` (string), `path` (string), `lines` (optional int, default 100)
- Validates path against `allowed_paths_read` for that server's role
- Uses `head -n {lines} {path}` under the hood

**7. `docker_ps`**
- List running containers
- Input: `server` (string), `all` (optional bool, include stopped)
- Formatted output

**8. `docker_logs`**
- Fetch container logs
- Input: `server` (string), `container` (string), `lines` (optional int, default 100), `since` (optional, e.g. "1h")

**9. `service_status`**
- Check systemd service status
- Input: `server` (string), `service` (string)
- Runs `systemctl status {service}`

**10. `service_journal`**
- Read systemd journal for a service
- Input: `server` (string), `service` (string), `lines` (optional int, default 50), `since` (optional)

## System Prompt

Build the system prompt dynamically from the inventory. Template:

```
You are an infrastructure management assistant for Galaxy Gaming Host.
You are running on the bastion server and have SSH access to downstream servers.

## Your Rules
1. NEVER fabricate or guess command output. Always use tools to get real data.
2. Read-only operations can be run freely. Destructive operations require operator approval.
3. If you're unsure about something, say so. Check first, act second.
4. When diagnosing issues, gather information systematically before suggesting fixes.
5. Always explain what you're about to do before doing it.
6. If a command fails, share the error output and suggest next steps.

## Available Servers
{generated_server_inventory_with_roles_and_services}

## Available Tools
{auto_generated_from_tool_registry}

## Response Style
- Be direct and concise
- Lead with the answer/finding, then explain
- Use code blocks for command output
- Flag anything that looks abnormal in metrics or logs
```

## Testing Strategy

- **Unit tests** for allowlist pattern matching, sanitizer rejection, inventory loading
- **Unit tests** for approval classification (which operations trigger approval)
- **Integration tests** with mocked SSH connections (use asyncssh mock or parameterized fixtures)
- **Integration tests** with mocked Anthropic API responses (test the tool dispatch loop)
- Do NOT write tests that require real infrastructure or real API calls

## Code Style & Standards

- Type hints everywhere (use `from __future__ import annotations`)
- Pydantic v2 for all config/data models
- async/await for all I/O operations
- No `shell=True` anywhere, ever. Use `subprocess_exec` with argument lists.
- `structlog` for all logging, JSON output to audit log file
- Docstrings on all public methods
- Keep functions focused — under 50 lines preferred, 100 max
- Handle errors explicitly, never bare `except:`

## What NOT to Build (yet)

- Discord bot integration (Phase 2)
- Web UI (Phase 2)
- WHMCS integration (Phase 2)
- Automated scheduled tasks / cron (Phase 2)
- Multi-user auth / RBAC (Phase 2, single operator for now)

## Environment Variables

```
ANTHROPIC_API_KEY=sk-ant-...          # Required
BASTION_AGENT_CONFIG=./config/        # Config directory path
BASTION_AGENT_LOG_LEVEL=INFO          # Logging level
```

## Build Order

Build in this order. Each step should be functional before moving to the next:

1. **Project scaffold** — pyproject.toml, package structure, config loading with Pydantic
2. **Security layer** — sanitizer, allowlist engine, approval gate, audit logger
3. **Tool base + registry** — base class, registration, schema export, dispatch
4. **Local tools** — run_local_command, read_file (bastion only, no SSH yet)
5. **Anthropic client + conversation loop** — connect it all, test with local tools only
6. **Terminal UI** — Rich-based interface with tool call display
7. **SSH tools** — asyncssh remote execution, per-host key management
8. **Remaining tools** — docker, systemd, monitoring queries
9. **Tests** — unit + integration with mocks
10. **Setup scripts** — bastion hardening, downstream prep, SSH key generation
