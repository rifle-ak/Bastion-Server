# Bastion Server — Developer Guide

## What This Is

An AI infrastructure management agent for Galaxy Gaming Host. Claude gets a set of tools to run commands, read files, and check server status — but everything goes through a security pipeline that sanitizes, allowlists, logs, and (optionally) asks for human approval before execution.

**Current state:** Security layer and local tools are complete and tested. The interactive agent session (build step 5) and SSH remote execution (build step 7) are not yet implemented.

## Project Structure

```
agent/                    # All application code lives here
├── main.py               # CLI entry (Click): `run` and `check-config` commands
├── config.py             # Pydantic v2 models for all YAML config
├── inventory.py          # Server inventory + role-based permission lookups
├── security/
│   ├── sanitizer.py      # Input rejection — blocks shell injection, path traversal
│   ├── allowlist.py      # Command glob matching + path ACLs per role
│   ├── approval.py       # Human approval gate for destructive ops
│   └── audit.py          # JSONL audit logger — every action, no exceptions
├── tools/
│   ├── base.py           # Abstract BaseTool + ToolResult dataclass
│   ├── registry.py       # Tool dispatch — wires security pipeline to execution
│   ├── local.py          # run_local_command (asyncio subprocess, never shell=True)
│   ├── files.py          # read_file (local + remote stub)
│   └── server_info.py    # list_servers, get_server_status
└── ui/                   # Terminal UI placeholder (Rich-based, not yet built)

config/                   # YAML configuration (all validated by Pydantic)
├── agent.yaml            # Model, timeouts, approval mode, audit path
├── servers.yaml          # Server inventory (hosts, roles, SSH keys, services)
└── permissions.yaml      # RBAC: allowed commands, paths, approval triggers

tests/                    # pytest suite — 100+ tests
├── conftest.py           # Shared fixtures
├── test_sanitizer.py     # Input rejection coverage
├── test_allowlist.py     # Command + path permission tests
├── test_approval.py      # Approval gate behavior
└── test_audit.py         # Audit logging format + content
```

## Tech Stack

| What | Why |
|------|-----|
| Python 3.12+ | Runtime |
| Click 8.1 | CLI framework |
| Pydantic 2.5+ | Config validation (strict mode) |
| PyYAML 6.0 | Config file parsing (safe_load only) |
| asyncio + asyncssh | Async execution + future SSH support |
| structlog | Structured JSON logging |
| Rich | Terminal formatting and approval prompts |
| anthropic 0.39 | Claude API client |
| Ruff | Linting and formatting |
| pytest + pytest-asyncio | Testing |

## Security Pipeline

Every tool invocation goes through this pipeline in order:

1. **Sanitizer** (`security/sanitizer.py`) — Hard rejects dangerous input. No escaping, no sanitizing, just denial. Blocks: `;`, `|`, `&`, `$()`, backticks, `..`, `eval`, `exec`, null bytes, newlines.

2. **Audit** (`security/audit.py`) — Logs the attempt to `logs/audit.jsonl` before execution. Always. Every attempt, denial, success, error, and timeout gets a timestamped JSONL record.

3. **Allowlist** (`security/allowlist.py`) — Commands checked against glob patterns (fnmatch) per role. Paths checked against role-based read/write directories. If it's not explicitly allowed, it's denied.

4. **Approval** (`security/approval.py`) — Commands matching patterns like "restart", "stop", "rm", "delete" require interactive confirmation. In `auto-deny` mode, these are refused automatically.

5. **Execution** — `asyncio.create_subprocess_exec` with timeout. Never `shell=True`. Commands are split with `shlex`.

## Configuration

Three YAML files in `config/`, all validated by Pydantic on load:

- **`agent.yaml`** — Model selection, token limits, timeout, approval mode
- **`servers.yaml`** — Server definitions with host, role, SSH config, services
- **`permissions.yaml`** — Per-role command allowlists, path ACLs, and approval trigger patterns

Validate everything with: `python -m agent.main check-config`

Environment variables:
- `ANTHROPIC_API_KEY` — Required for agent sessions
- `BASTION_AGENT_CONFIG` — Config directory path (default: `./config`)
- `BASTION_AGENT_LOG_LEVEL` — DEBUG, INFO, WARNING, ERROR (default: INFO)

## Running Tests

```bash
pytest                          # Full suite
pytest --cov                    # With coverage
pytest tests/test_allowlist.py  # Single module
```

Tests are async-aware (pytest-asyncio). If you touch anything in `agent/security/`, run the full suite — the security tests are comprehensive and intentionally overlapping.

## Development Workflow

1. Create a virtual environment: `python3 -m venv .venv && source .venv/bin/activate`
2. Install deps: `pip install -r requirements.txt`
3. Make changes
4. Run tests: `pytest`
5. Validate config if you touched YAML: `python -m agent.main check-config`

## Key Design Decisions

- **Allowlist over blocklist** — Nothing runs unless explicitly permitted. This is intentional and non-negotiable.
- **Never `shell=True`** — All subprocess calls use exec with argument arrays. Shell injection is structurally impossible at the execution layer.
- **Rejection over sanitization** — The sanitizer doesn't try to clean input. It rejects it. Escaping is a game you eventually lose.
- **Audit everything** — The audit logger cannot be bypassed. It runs before the allowlist check, so even denied attempts are recorded.
- **Defense in depth** — The sanitizer and allowlist both check for shell metacharacters independently. Redundancy is the point.
