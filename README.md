# Bastion Agent

Infrastructure management agent for **Galaxy Gaming Host**, powered by the Anthropic Claude API with native tool use.

Runs on a hardened bastion server and provides SSH-based access to downstream servers — executing commands, querying monitoring, inspecting Docker containers, reading logs, and performing administrative tasks with structured audit logging and human approval gates for destructive operations.

Think of it as giving Claude a very strict, very paranoid set of keys to your servers. It can check on things, read logs, and restart services, but only the ones you explicitly allow, and it has to ask permission before doing anything scary.

## Fresh Server Install

Starting from a clean Ubuntu 24.04 box? Here's everything you need, start to finish.

### Prerequisites

- **Ubuntu 24.04** (or any Linux with Python 3.12+)
- **An Anthropic API key** (you're going to need one of these, no way around it)
- **SSH access** to your downstream servers (the ones you want the agent to manage)

### Step 1: System dependencies

```bash
# Python 3.12 ships with Ubuntu 24.04, but just in case
sudo apt update && sudo apt install -y python3 python3-pip python3-venv git
```

### Step 2: Clone and install

```bash
git clone https://github.com/rifle-ak/Bastion-Server.git
cd Bastion-Server

# Install as an editable package (so `bastion-agent` CLI works)
pip install -e ".[dev]"

# Or if you prefer pinned versions for reproducibility
pip install -r requirements.txt
pip install -e .
```

### Step 3: Create the agent user

The agent connects to downstream servers as `claude-agent`. Create this user on each server you want to manage:

```bash
# On each downstream server
sudo useradd -m -s /bin/bash claude-agent
```

Give it minimal sudo permissions for the commands you actually need. Don't give it root. Don't do it. You'll regret it at 3am when you're reading audit logs trying to figure out what happened.

### Step 4: Set up SSH keys

One keypair per downstream server. Ed25519, because it's not 2005:

```bash
# On the bastion server
mkdir -p ~/.ssh/keys

# Generate a key for each server
ssh-keygen -t ed25519 -f ~/.ssh/keys/gameserver-01_ed25519 -N "" -C "bastion-agent@gameserver-01"
ssh-keygen -t ed25519 -f ~/.ssh/keys/monitoring_ed25519 -N "" -C "bastion-agent@monitoring"

# Copy public keys to each server
ssh-copy-id -i ~/.ssh/keys/gameserver-01_ed25519.pub claude-agent@10.0.1.10
ssh-copy-id -i ~/.ssh/keys/monitoring_ed25519.pub claude-agent@10.0.1.20
```

The key paths in `config/servers.yaml` default to `~/.ssh/keys/<servername>_ed25519`. If you put them somewhere else, update the config.

### Step 5: Configure

Configuration lives in `config/`. The defaults are already set up for the Galaxy Gaming Host infrastructure, but review them:

| File | What it does |
|------|-------------|
| `config/agent.yaml` | Agent behavior: model, timeouts, approval mode |
| `config/servers.yaml` | Server inventory: hosts, roles, SSH keys, services |
| `config/permissions.yaml` | Per-role allowed commands, paths, and approval patterns |

The permissions file is the important one. It defines exactly what commands each server role can run. The defaults are conservative — you can always loosen them later, but you can't un-`rm -rf` something.

### Step 6: Set your API key

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Or throw it in a `.env` file (gitignored, so you won't accidentally publish it to the world):

```bash
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env
```

### Step 7: Validate and run

```bash
# Make sure your config is valid before you trust it with your servers
bastion-agent check-config

# Start an interactive session
bastion-agent run

# Custom config directory (if you moved it)
bastion-agent run --config-dir /etc/bastion-agent/
```

Optional environment variables:

```bash
export BASTION_AGENT_CONFIG=./config    # Config directory (default: ./config)
export BASTION_AGENT_LOG_LEVEL=INFO     # DEBUG, INFO, WARNING, ERROR
```

## Architecture

```
User <-> CLI (Rich) <-> Conversation Manager <-> Anthropic API (tool use)
                                                        |
                                                  Tool Router
                                             |        |        |
                                        Security   Tool Impls  Audit Log
                                        (allowlist, (local,ssh, (JSON)
                                         approval)  docker,
                                                    metrics)
                                                       | SSH
                                                Downstream Servers
```

## Security Model

This is the part that lets you sleep at night:

- **Allowlisting, not blocklisting** — only explicitly permitted operations can execute
- **Input sanitization** — shell metacharacters are rejected, not escaped (escaping is a game you eventually lose)
- **Human approval gate** — destructive operations (restart, stop, rm, etc.) require operator confirmation
- **Structured audit logging** — every tool call is logged as JSON before execution, no exceptions
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
