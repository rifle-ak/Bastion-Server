# Bastion Agent

Infrastructure management agent for **Galaxy Gaming Host**, powered by the Anthropic Claude API with native tool use.

Runs on a hardened bastion server and provides SSH-based access to downstream servers — executing commands, querying monitoring, inspecting Docker containers, reading logs, and performing administrative tasks with structured audit logging and human approval gates for destructive operations.

## Quick Start

### Prerequisites

- Python 3.11+
- An Anthropic API key

### Installation

```bash
# Clone the repo
git clone https://github.com/rifle-ak/Bastion-Server.git
cd Bastion-Server

# Install with dependencies
pip install -e ".[dev]"

# Or from pinned requirements
pip install -r requirements.txt
pip install -e .
```

### Configuration

Configuration lives in the `config/` directory:

| File | Purpose |
|---|---|
| `config/agent.yaml` | Agent behavior: model, timeouts, approval mode |
| `config/servers.yaml` | Server inventory: hosts, roles, SSH keys, services |
| `config/permissions.yaml` | Per-role allowed commands, paths, and approval patterns |

Set your API key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Optional environment variables:

```bash
export BASTION_AGENT_CONFIG=./config    # Config directory (default: ./config)
export BASTION_AGENT_LOG_LEVEL=INFO     # DEBUG, INFO, WARNING, ERROR
```

### Usage

```bash
# Validate configuration
bastion-agent check-config

# Start an interactive session
bastion-agent run

# Use a custom config directory
bastion-agent run --config-dir /etc/bastion-agent/
```

## Architecture

```
User ↔ CLI (Rich) ↔ Conversation Manager ↔ Anthropic API (tool use)
                                                    ↕
                                              Tool Router
                                         ↕        ↕        ↕
                                    Security   Tool Impls  Audit Log
                                    (allowlist, (local,ssh, (JSON)
                                     approval)  docker,
                                                metrics)
                                                   ↕ SSH
                                            Downstream Servers
```

## Security Model

- **Allowlisting, not blocklisting** — only explicitly permitted operations can execute
- **Input sanitization** — shell metacharacters are rejected, not escaped
- **Human approval gate** — destructive operations (restart, stop, rm, etc.) require operator confirmation
- **Structured audit logging** — every tool call is logged as JSON before execution
- **Scoped file access** — reads/writes restricted to configured paths per server role
- **Per-host SSH keys** — dedicated keypairs for each downstream server

## Project Structure

```
├── agent/                  # Main package
│   ├── main.py             # Click CLI entry point
│   ├── config.py           # Pydantic config models + YAML loading
│   ├── inventory.py        # Server inventory model
│   ├── client.py           # Anthropic API client + conversation loop
│   ├── prompts.py          # System prompt builder
│   ├── tools/              # Tool implementations
│   ├── security/           # Allowlist, approval, audit, sanitizer
│   └── ui/                 # Rich terminal interface
├── config/                 # YAML configuration files
├── tests/                  # pytest test suite
├── scripts/                # Server setup scripts
└── logs/                   # Audit logs (gitignored)
```

## Development

```bash
# Run tests
pytest

# Run with coverage
pytest --cov=agent

# Validate config
bastion-agent check-config --config-dir ./config
```

## License

MIT
