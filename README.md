# Bastion Agent

Infrastructure management agent for **Galaxy Gaming Host**, powered by the Anthropic Claude API with native tool use.

Runs on a hardened bastion server and provides intelligent SSH-based access to your downstream servers — executing commands, querying monitoring, inspecting Docker containers, reading logs, and performing administrative tasks. Every action goes through structured audit logging, command allowlisting, and a human approval gate for anything destructive.

---
Think of it as giving Claude a very strict, very paranoid set of keys to your servers. It can check on things, read logs, and restart services, but only the ones you explicitly allow, and it has to ask permission before doing anything scary.

## Fresh Server Install

Starting from a clean Ubuntu 24.04 box? Here's everything you need, start to finish.

## System Requirements

| Requirement | Minimum | Recommended |
|---|---|---|
| **OS** | Ubuntu 22.04 / Debian 12 | Ubuntu 24.04 LTS |
| **Python** | 3.11 | 3.12+ |
| **Memory** | 512 MB | 1 GB+ |
| **Disk** | 500 MB free | 1 GB+ free |
| **Network** | Outbound HTTPS (API) | + SSH to downstream hosts |
| **Privileges** | Root (for install) | Runs as unprivileged `claude-agent` user |

### Required Accounts

- **Anthropic API key** — get one at [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys)

### Required Ports

| Port | Direction | Purpose |
|---|---|---|
| 22 | Inbound | SSH access to the bastion |
| 443 | Outbound | Anthropic API (api.anthropic.com) |
| 22 | Outbound | SSH to downstream servers |
| 8428 | Outbound (optional) | VictoriaMetrics queries |

---

## Installation

SSH into the server you want to use as your bastion, then run:

```bash
curl -fsSL https://raw.githubusercontent.com/rifle-ak/Bastion-Server/main/install.sh | sudo bash
```

That's it. The installer handles everything:
- Installs system packages (Python 3, git, pip, venv)
- Creates a locked-down `claude-agent` user
- Clones the repo to `/opt/bastion-agent`
- Sets up a Python virtualenv with all dependencies
- Copies config files to `/etc/bastion-agent/`
- Installs a systemd service
- Hardens SSH (disables root login, disables password auth)
- Enables UFW firewall (if available)

### Inspect Before Running

If you want to read the script first:

```bash
curl -fsSL https://raw.githubusercontent.com/rifle-ak/Bastion-Server/main/install.sh -o install.sh
less install.sh
sudo bash install.sh
```

### Install Options

Pass environment variables to customize the install:

```bash
# Auto-configure the API key during install
sudo ANTHROPIC_API_KEY=sk-ant-... bash install.sh

# Install from a specific branch
sudo BRANCH=dev bash install.sh

# Install to a custom directory
sudo INSTALL_DIR=/srv/bastion bash install.sh
```

---

## Post-Install Setup

After the installer finishes, you need to do three things.

### 1. Set Your API Key
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
sudo nano /etc/bastion-agent/env
```

Replace the placeholder with your real key:

```
ANTHROPIC_API_KEY=sk-ant-api03-your-real-key-here
```

### 2. Edit the Server Inventory

Tell the agent about your infrastructure:

```bash
sudo nano /etc/bastion-agent/servers.yaml
```

```yaml
servers:
  localhost:
    host: localhost
    role: bastion
    user: claude-agent
    description: "Bastion server (this machine)"
    ssh: false

  gameserver-01:
    host: 10.0.1.10
    role: game-server
    user: claude-agent
    key_path: /home/claude-agent/.ssh/keys/gameserver-01_ed25519
    description: "Primary game server — Pterodactyl Wings"
    services:
      - pterodactyl-wings
      - docker

  monitoring:
    host: 10.0.1.20
    role: monitoring
    user: claude-agent
    key_path: /home/claude-agent/.ssh/keys/monitoring_ed25519
    description: "VictoriaMetrics + Grafana stack"
    services:
      - victoriametrics
      - grafana
    metrics_url: http://10.0.1.20:8428
```

### 3. Set Up SSH Keys for Downstream Servers

Generate per-host keypairs:

```bash
cd /opt/bastion-agent
sudo bash scripts/generate-ssh-keys.sh
```
Or throw it in a `.env` file (gitignored, so you won't accidentally publish it to the world):

```bash
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env
```

### Step 7: Validate and run

```bash
# Make sure your config is valid before you trust it with your servers
bastion-agent check-config

This reads your `servers.yaml` and generates an Ed25519 keypair for each SSH-enabled server. Then copy the public keys to each downstream host:

```bash
sudo -u claude-agent ssh-copy-id -i /home/claude-agent/.ssh/keys/gameserver-01_ed25519 claude-agent@10.0.1.10
sudo -u claude-agent ssh-copy-id -i /home/claude-agent/.ssh/keys/monitoring_ed25519 claude-agent@10.0.1.20
```

Each downstream server needs the `claude-agent` user created. Run on each downstream host:

```bash
curl -fsSL https://raw.githubusercontent.com/rifle-ak/Bastion-Server/main/scripts/setup-downstream.sh | sudo bash
```

---

## Usage

### Interactive Mode (Recommended for First Run)

```bash
sudo -u claude-agent \
  ANTHROPIC_API_KEY=sk-ant-... \
  /opt/bastion-agent/venv/bin/bastion-agent run \
  --config-dir /etc/bastion-agent
```

You'll see the banner and a prompt:

```
╭──────────────────── Galaxy Gaming Host ────────────────────╮
│ Bastion Agent v0.1.0                                       │
│ Model: claude-sonnet-4-5-20250514                          │
│ Servers: localhost, gameserver-01, monitoring               │
│ Type /quit or /exit to end the session.                    │
╰────────────────────────────────────────────────────────────╯

[bastion] > what's the disk usage on gameserver-01?
```

### Run as a Service
# Custom config directory (if you moved it)
bastion-agent run --config-dir /etc/bastion-agent/
```

Optional environment variables:

```bash
export BASTION_AGENT_CONFIG=./config    # Config directory (default: ./config)
export BASTION_AGENT_LOG_LEVEL=INFO     # DEBUG, INFO, WARNING, ERROR
```

## Architecture

```bash
sudo systemctl enable --now bastion-agent
```

View logs:

```bash
sudo journalctl -u bastion-agent -f
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

### Validate Configuration

Check your config files for errors without starting the agent:

```bash
/opt/bastion-agent/venv/bin/bastion-agent check-config --config-dir /etc/bastion-agent
```

---

## Tools

The agent has 10 built-in tools. Claude picks the right one based on your request.

| Tool | What It Does | Needs Approval |
|---|---|---|
| `run_local_command` | Execute a command on the bastion server | If destructive |
| `run_remote_command` | Execute a command on a downstream server via SSH | If destructive |
| `read_file` | Read a file (scoped to allowed paths per role) | No |
| `list_servers` | Show the full server inventory | No |
| `get_server_status` | Quick health check: uptime, disk, memory | No |
| `docker_ps` | List running containers on a server | No |
| `docker_logs` | Fetch container logs (with tail/since) | No |
| `service_status` | Check systemd service status | No |
| `service_journal` | Read systemd journal for a service | No |
| `query_metrics` | PromQL query against VictoriaMetrics | No |

---

## Security Model

### Allowlisting, Not Blocklisting

Every command must match an explicit pattern in `permissions.yaml` before it can execute. Unknown commands are rejected by default.

```yaml
roles:
  game-server:
    allowed_commands:
      - "uptime"
      - "docker ps *"
      - "docker logs *"
      - "docker restart *"    # requires operator approval
      - "systemctl status *"
```

### Input Sanitization

Shell metacharacters are **rejected, not escaped**. The following are blocked in all command arguments:

| Pattern | Reason |
|---|---|
| `;` `\|` `&` | Command chaining |
| `$()` `` ` `` | Command substitution |
| `..` | Path traversal |
| `> /` `>> /` | Redirect to absolute path |
| `eval` `exec` | Code execution |

### Human Approval Gate

Destructive operations require you to confirm before they execute. The agent shows exactly what it wants to run and waits for your yes/no:

```
╭──── Tool Call: run_remote_command ─────╮
│ {                                      │
│   "server": "gameserver-01",           │
│   "command": "docker restart pterod…"  │
│ }                                      │
╰────────────────────────────────────────╯
⚠ This operation requires approval.
  Allow "docker restart pterodactyl-wings" on gameserver-01? [y/N]
```

Patterns that trigger approval: `restart`, `stop`, `kill`, `rm`, `remove`, `delete`, `drop`, `truncate`, `write`, `tee`, `>`, `>>`

### Audit Logging

Every tool call is logged to `/var/log/bastion-agent/audit.jsonl` as structured JSON **before execution**:

```json
{"timestamp":"2025-01-15T14:32:01Z","event":"tool_attempt","tool":"run_remote_command","input":{"server":"gameserver-01","command":"docker ps"},"user":"operator"}
{"timestamp":"2025-01-15T14:32:02Z","event":"tool_success","tool":"run_remote_command","exit_code":0}
```

### Per-Host SSH Keys

Each downstream server gets its own Ed25519 keypair. No shared keys. Keys are stored in `/home/claude-agent/.ssh/keys/` with mode `600`.

---

## Configuration Reference

All config lives in `/etc/bastion-agent/` (or `./config/` for local dev).

### `agent.yaml` — Agent Behavior

```yaml
model: claude-sonnet-4-5-20250514    # Claude model to use
max_tokens: 4096                      # Max response tokens
max_tool_iterations: 10               # Safety limit on tool call rounds
command_timeout: 30                   # Default command timeout (seconds)
audit_log_path: /var/log/bastion-agent/audit.jsonl
approval_mode: interactive            # "interactive" or "auto_deny"
```

### `servers.yaml` — Server Inventory

```yaml
servers:
  <name>:
    host: <ip-or-hostname>            # required
    role: <role-name>                 # required — maps to permissions.yaml
    user: claude-agent                # SSH username (default: claude-agent)
    description: "Human description"  # shown to Claude in system prompt
    ssh: true                         # false for localhost only
    key_path: ~/.ssh/keys/<name>_ed25519
    services:                         # optional — listed in system prompt
      - docker
      - nginx
    metrics_url: http://host:8428     # optional — for query_metrics tool
    known_hosts_path: null            # optional — SSH known_hosts file
```

### `permissions.yaml` — Access Control

```yaml
roles:
  <role-name>:
    allowed_commands:                 # glob patterns (* = wildcard)
      - "uptime"
      - "docker ps *"
    allowed_paths_read:               # directory prefixes
      - /var/log/
      - /etc/
    allowed_paths_write: []           # empty = no writes allowed

approval_required_patterns:           # substrings that trigger approval
  - "restart"
  - "stop"
  - "rm "
```

---

## Architecture

```
┌───────────────────────────────────────────────────────────────┐
│                      BASTION SERVER                           │
│                                                               │
│  User ──► CLI (Rich) ──► Conversation Manager                │
│                               │                               │
│                          Anthropic API                        │
│                          (tool use)                           │
│                               │                               │
│                         Tool Router                           │
│                        /     |     \                          │
│                   Security  Tools  Audit Log                  │
│                   ┌──────┐  │      (JSON)                    │
│                   │Allow-│  ├─ local commands                 │
│                   │list  │  ├─ remote commands (SSH)          │
│                   │      │  ├─ docker ps/logs                 │
│                   │Sanit-│  ├─ systemd status/journal         │
│                   │izer  │  ├─ file reads                     │
│                   │      │  ├─ metrics queries                │
│                   │Appro-│  └─ server inventory               │
│                   │val   │                                    │
│                   └──────┘                                    │
│                               │                               │
│                          SSH (per-host keys)                  │
│                        /      |        \                      │
│                   ┌────┐  ┌───────┐  ┌──────────┐           │
│                   │Game│  │Monitor│  │Other     │           │
│                   │Srvs│  │Stack  │  │Downstream│           │
│                   └────┘  └───────┘  └──────────┘           │
└───────────────────────────────────────────────────────────────┘
```

---
This is the part that lets you sleep at night:

- **Allowlisting, not blocklisting** — only explicitly permitted operations can execute
- **Input sanitization** — shell metacharacters are rejected, not escaped (escaping is a game you eventually lose)
- **Human approval gate** — destructive operations (restart, stop, rm, etc.) require operator confirmation
- **Structured audit logging** — every tool call is logged as JSON before execution, no exceptions
- **Scoped file access** — reads/writes restricted to configured paths per server role
- **Per-host SSH keys** — dedicated keypairs for each downstream server

## Project Structure

```
bastion-agent/
├── install.sh                       # One-command installer
├── pyproject.toml                   # Package metadata & dependencies
├── requirements.txt                 # Pinned dependency versions
│
├── agent/                           # Main Python package
│   ├── __init__.py                  # Version
│   ├── main.py                      # CLI entry point (Click)
│   ├── client.py                    # Anthropic API + conversation loop
│   ├── config.py                    # Pydantic config models + YAML loader
│   ├── inventory.py                 # Server inventory model
│   ├── prompts.py                   # Dynamic system prompt builder
│   ├── tools/
│   │   ├── base.py                  # BaseTool protocol + ToolResult
│   │   ├── registry.py             # Registration, schema gen, dispatch pipeline
│   │   ├── local.py                 # run_local_command
│   │   ├── remote.py               # run_remote_command (asyncssh)
│   │   ├── files.py                 # read_file (local + remote)
│   │   ├── server_info.py          # list_servers, get_server_status
│   │   ├── docker_tools.py         # docker_ps, docker_logs
│   │   ├── systemd.py              # service_status, service_journal
│   │   └── monitoring.py           # query_metrics (VictoriaMetrics)
│   ├── security/
│   │   ├── allowlist.py             # Command pattern matching engine
│   │   ├── approval.py             # Human-in-the-loop gate
│   │   ├── audit.py                 # Structured JSON audit logger
│   │   └── sanitizer.py            # Shell injection prevention
│   └── ui/
│       └── terminal.py             # Rich terminal interface
│
├── config/                          # Default config files
│   ├── agent.yaml
│   ├── servers.yaml
│   └── permissions.yaml
│
├── scripts/
│   ├── setup-bastion.sh             # Bastion hardening (called by install.sh)
│   ├── setup-downstream.sh         # Downstream server prep
│   └── generate-ssh-keys.sh        # Per-host Ed25519 key generation
│
├── systemd/
│   └── bastion-agent.service        # Systemd unit file
│
├── tests/                           # 78 tests, all mocked (no real infra)
│   ├── test_sanitizer.py
│   ├── test_allowlist.py
│   ├── test_approval.py
│   ├── test_inventory.py
│   └── test_tools.py
│
└── logs/                            # Audit logs (gitignored)
```

---

## Development

### Local Dev Setup

```bash
git clone https://github.com/rifle-ak/Bastion-Server.git
cd Bastion-Server
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Run Tests

```bash
pytest                    # all 78 tests
pytest -v                 # verbose
pytest --cov=agent        # with coverage report
```

### Validate Config

```bash
bastion-agent check-config --config-dir ./config
```

### Run Locally (Dev)

```bash
export ANTHROPIC_API_KEY=sk-ant-...
bastion-agent run --config-dir ./config
```

---

## Troubleshooting

### "ANTHROPIC_API_KEY environment variable is not set"

Set the key in the env file:

```bash
sudo nano /etc/bastion-agent/env
```

Or pass it directly:

```bash
sudo -u claude-agent ANTHROPIC_API_KEY=sk-ant-... /opt/bastion-agent/venv/bin/bastion-agent run --config-dir /etc/bastion-agent
```

### "SSH connection failed" to a downstream server

1. Verify the key exists: `ls -la /home/claude-agent/.ssh/keys/`
2. Test manually: `sudo -u claude-agent ssh -i /home/claude-agent/.ssh/keys/<server>_ed25519 claude-agent@<host>`
3. Check the downstream server has the `claude-agent` user and the public key in `~/.ssh/authorized_keys`

### "Operation not permitted by security policy"

The command doesn't match any pattern in `permissions.yaml`. Add it to the appropriate role's `allowed_commands` list.

### Service won't start

```bash
sudo journalctl -u bastion-agent -n 50 --no-pager
```

Common causes:
- Missing or invalid API key in `/etc/bastion-agent/env`
- Syntax error in a YAML config file
- Wrong file permissions (configs must be readable by `claude-agent`)

---

## Uninstall

```bash
sudo systemctl stop bastion-agent
sudo systemctl disable bastion-agent
sudo rm /etc/systemd/system/bastion-agent.service
sudo systemctl daemon-reload
sudo rm -rf /opt/bastion-agent
sudo rm -rf /etc/bastion-agent
sudo userdel -r claude-agent
```

---

## License

MIT
