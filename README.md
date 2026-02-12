# Bastion Server

An AI-powered infrastructure management agent for Galaxy Gaming Host. Think of it as giving Claude a very strict, very paranoid set of keys to your servers — it can check on things, read logs, and restart services, but only the ones you explicitly allow, and it has to ask permission before doing anything scary.

## What It Does

Bastion Agent sits on a bastion host and manages your infrastructure through a multi-layered security pipeline. Every command goes through:

```
Tool Call -> Sanitize -> Audit Log -> Allowlist Check -> Human Approval -> Execute
```

No shortcuts. No exceptions. Every single action is logged, allowlisted, and (for anything destructive) requires your explicit approval. If Claude tries to get creative with shell metacharacters or path traversal, it gets shut down before anything touches the OS.

**Currently manages:**
- **Bastion host** (localhost) — the machine running the agent
- **Game server** (Pterodactyl Wings + Docker) — your Rust server
- **Monitoring stack** (VictoriaMetrics + Grafana) — keeping an eye on everything

## Fresh Server Install

Starting from a clean Linux box? Here's what you need.

### Prerequisites

- **Python 3.8+** (3.9 recommended)
- **pip** (comes with Python, but just in case)
- **git** (you probably already have this)
- **SSH keys** for your remote servers (ed25519, because it's not 2005)

### Step 1: Clone and set up the environment

```bash
git clone https://github.com/rifle-ak/Bastion-Server.git
cd Bastion-Server

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Step 2: Set up your SSH keys

The agent connects to remote servers as `claude-agent`. You'll need SSH keys for each remote host. The default paths are configured in `config/servers.yaml`:

```bash
mkdir -p ~/.ssh/keys

# Generate keys for each server (or copy existing ones)
ssh-keygen -t ed25519 -f ~/.ssh/keys/gameserver-01_ed25519 -N ""
ssh-keygen -t ed25519 -f ~/.ssh/keys/monitoring_ed25519 -N ""

# Copy the public keys to each server
ssh-copy-id -i ~/.ssh/keys/gameserver-01_ed25519.pub claude-agent@10.0.1.10
ssh-copy-id -i ~/.ssh/keys/monitoring_ed25519.pub claude-agent@10.0.1.20
```

Make sure the `claude-agent` user exists on each remote server with appropriate (minimal) sudo permissions. Don't give it root. Seriously.

### Step 3: Configure

All configuration lives in `config/`. The files that ship with the repo are already set up for the Galaxy Gaming Host infrastructure, but you'll want to review them:

**`config/agent.yaml`** — Agent behavior settings:
```yaml
model: claude-sonnet-4-5-20250514   # The brain
max_tokens: 4096                     # Response size limit
max_tool_iterations: 10              # Max back-and-forth per session
command_timeout: 30                  # Seconds before a command is killed
audit_log_path: ./logs/audit.jsonl   # Where every action gets logged
approval_mode: interactive           # "interactive" or "auto-deny"
```

**`config/servers.yaml`** — Your server inventory. Add/remove servers here. Each server needs a `role` that maps to permissions.

**`config/permissions.yaml`** — The big one. This defines what commands each role can run, what paths it can read, and what patterns trigger the "are you sure?" approval prompt. The defaults are conservative — you can always loosen them later, but you can't un-`rm -rf` something.

### Step 4: Validate your config

Before you run anything:

```bash
python -m agent.main check-config
```

This will parse all three YAML files, validate them against the Pydantic models, and tell you if anything's misconfigured. Fix any errors before proceeding.

### Step 5: Set up the Anthropic API key

```bash
export ANTHROPIC_API_KEY="your-key-here"
```

Or put it in a `.env` file (which is gitignored, so you won't accidentally commit it):

```bash
echo 'ANTHROPIC_API_KEY=your-key-here' > .env
```

### Step 6: Run it

```bash
python -m agent.main run
```

> **Note:** The interactive agent session is still under development (build step 5). Currently the security pipeline, tool registry, and local command execution are fully functional and tested.

## Security Architecture

This isn't just "run commands on a server." There are five layers between a tool call and actual execution:

| Layer | What It Does | How It Fails |
|-------|-------------|--------------|
| **Sanitizer** | Rejects shell injection, command chaining, path traversal, null bytes | Hard reject — no escaping, no sanitizing, just "no" |
| **Audit Logger** | Records every attempt (success, denied, error, timeout) as JSONL | It doesn't fail. Everything gets logged. Always. |
| **Allowlist** | Checks commands against glob patterns and paths against role-based ACLs | Denied if not explicitly permitted — allowlist, not blocklist |
| **Approval Gate** | Prompts you for confirmation on destructive operations (restart, stop, rm, etc.) | In `auto-deny` mode, anything needing approval is automatically refused |
| **Executor** | Runs the command with `asyncio.create_subprocess_exec` (never `shell=True`) with timeout | Timeout kills the process after 30s (configurable) |

Rejected patterns include: `;`, `|`, `&`, `$()`, `` ` ``, `..`, `eval`, `exec`, null bytes, and newline injection.

## Project Structure

```
Bastion-Server/
├── agent/                    # Application code
│   ├── __init__.py           # Version (0.1.0)
│   ├── main.py               # CLI entry point (Click)
│   ├── config.py             # Pydantic config models
│   ├── inventory.py          # Server inventory management
│   ├── security/             # The paranoia layer
│   │   ├── allowlist.py      # Command & path allowlisting
│   │   ├── approval.py       # Human approval gates
│   │   ├── audit.py          # JSONL audit logging
│   │   └── sanitizer.py      # Input rejection (not sanitization)
│   ├── tools/                # What the agent can actually do
│   │   ├── base.py           # Abstract tool interface
│   │   ├── registry.py       # Tool dispatch + security pipeline
│   │   ├── local.py          # Local command execution
│   │   ├── files.py          # File reading
│   │   └── server_info.py    # Server status queries
│   └── ui/                   # Terminal UI (placeholder)
├── config/                   # YAML configuration
│   ├── agent.yaml            # Agent behavior
│   ├── servers.yaml          # Server inventory
│   └── permissions.yaml      # RBAC + approval patterns
├── tests/                    # 100+ tests across 4 modules
├── logs/                     # Audit logs (gitignored)
├── requirements.txt          # Pinned dependencies
├── pyproject.toml            # Poetry/packaging config
└── bastion-agent.tar.gz      # Pre-packaged agent
```

## Running Tests

```bash
# Run the full suite
pytest

# With coverage
pytest --cov

# Specific module
pytest tests/test_allowlist.py -v
```

There are 100+ tests covering the security pipeline (sanitizer, allowlist, approval, audit). If you change anything in `agent/security/`, run the tests. All of them.

## Build Status

- [x] Project scaffold, config system, CLI
- [x] Security layer (sanitizer, allowlist, approval, audit)
- [x] Tool registry with security pipeline
- [x] Local tools (run_local_command, read_file, list_servers, get_server_status)
- [ ] Interactive agent session (the main loop)
- [ ] Terminal UI
- [ ] SSH remote execution

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `ANTHROPIC_API_KEY` | Your Anthropic API key | (required) |
| `BASTION_AGENT_CONFIG` | Path to config directory | `./config` |
| `BASTION_AGENT_LOG_LEVEL` | Log level (DEBUG/INFO/WARNING/ERROR) | `INFO` |

## License

MPL-2.0. See [LICENSE](LICENSE) for the full text.
