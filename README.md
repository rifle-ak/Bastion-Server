# Bastion Agent

Infrastructure management agent for **Galaxy Gaming Host**, powered by the Anthropic Claude API with native tool use.

Runs on a hardened bastion server and provides intelligent SSH-based access to your downstream servers — executing commands, querying monitoring, inspecting Docker containers, reading logs, and performing administrative tasks. Every action goes through structured audit logging, command allowlisting, and a human approval gate for anything destructive.

Think of it as giving Claude a very strict, very paranoid set of keys to your servers. It can check on things, read logs, and restart services, but only the ones you explicitly allow, and it has to ask permission before doing anything scary.

---

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

If you want to read the script first (and you should, you're piping curl into sudo bash):

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

#### Server Definition Fields

| Field | Required | Default | Description |
|---|---|---|---|
| `host` | Yes | — | IP address or hostname |
| `role` | Yes | — | Role name (maps to permissions.yaml) |
| `user` | No | `claude-agent` | SSH username |
| `description` | No | `""` | Human-readable description (shown to Claude) |
| `ssh` | No | `true` | Set `false` for localhost only |
| `key_path` | No | `null` | Path to SSH private key (supports `~` expansion) |
| `services` | No | `[]` | Services on this server (informational) |
| `metrics_url` | No | `null` | VictoriaMetrics endpoint URL |
| `metrics_auth` | No | `null` | Basic auth (`user:pass` or `$ENV_VAR_NAME`) |
| `known_hosts_path` | No | `null` | SSH known_hosts file |

#### Available Roles

| Role | What It Enables |
|---|---|
| `bastion` | Local management, broad read access, no writes |
| `game-server` | Pterodactyl Wings, Docker containers, game server diagnostics |
| `webhost` | cPanel/WHM API, Apache/LiteSpeed, MySQL, DNS, mail, WP-CLI |
| `monitoring` | Docker Compose for monitoring stack, metrics queries |
| `saltbox` | Docker Compose, Plex/Sonarr/Radarr container management |

#### Example: All Server Types

```yaml
servers:
  # ── Bastion (this machine) ──
  localhost:
    host: localhost
    role: bastion
    user: claude-agent
    description: "Bastion server (this machine)"
    ssh: false

  # ── Game Server (Pterodactyl Wings) ──
  gameserver-01:
    host: 10.0.1.10
    role: game-server
    user: claude-agent
    key_path: ~/.ssh/keys/gameserver-01_ed25519
    description: "Primary Rust/Minecraft server — Pterodactyl Wings"
    services:
      - pterodactyl-wings
      - docker

  # ── cPanel Webhosting Server ──
  webhost-01:
    host: 209.222.101.44
    role: webhost
    user: claude-agent
    key_path: ~/.ssh/keys/webhost-01_ed25519
    description: "Primary cPanel server — shared hosting"
    services:
      - cpanel
      - httpd
      - mysql
      - named
      - exim

  # ── Monitoring Stack ──
  monitoring:
    host: 10.0.1.20
    role: monitoring
    user: claude-agent
    key_path: ~/.ssh/keys/monitoring_ed25519
    description: "VictoriaMetrics + Grafana stack"
    services:
      - victoriametrics
      - grafana
      - vmagent
    metrics_url: http://10.0.1.20:8428

  # ── Saltbox Media Server ──
  media-01:
    host: 24.129.90.46
    role: saltbox
    user: claude-agent
    key_path: ~/.ssh/keys/media-01_ed25519
    description: "Saltbox media server — Plex, Sonarr, Radarr"
    services:
      - docker
      - plex
      - sonarr
      - radarr
```

#### Adding a New Server

1. Add the server entry to `servers.yaml` (choose the appropriate role)
2. Generate SSH keys: `sudo bash scripts/generate-ssh-keys.sh`
3. Copy the public key to the new server: `sudo -u claude-agent ssh-copy-id -i /home/claude-agent/.ssh/keys/<name>_ed25519 claude-agent@<host>`
4. If the server needs a new role, add it to `permissions.yaml`
5. Restart the agent: `sudo systemctl restart bastion-agent`

### 3. Set Up SSH Keys for Downstream Servers

Generate per-host keypairs:

```bash
cd /opt/bastion-agent
sudo bash scripts/generate-ssh-keys.sh
```

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
│ Model: claude-sonnet-4-5-20250929                          │
│ Servers: localhost, gameserver-01, monitoring               │
│ Type /quit or /exit to end the session.                    │
╰────────────────────────────────────────────────────────────╯

[bastion] > what's the disk usage on gameserver-01?
```

### Run as a Service

```bash
sudo systemctl enable --now bastion-agent
```

View logs:

```bash
sudo journalctl -u bastion-agent -f
```

### Validate Configuration

Check your config files for errors without starting the agent:

```bash
/opt/bastion-agent/venv/bin/bastion-agent check-config --config-dir /etc/bastion-agent
```

---

## Tools

The agent has **76 tools** across 12 categories. Claude picks the right one based on your request.

### Core Infrastructure (6 tools)

| Tool | What It Does | Approval |
|---|---|---|
| `run_local_command` | Execute a command on the bastion server | If destructive |
| `run_remote_command` | Execute a command on a downstream server via SSH | If destructive |
| `read_file` | Read a file (scoped to allowed paths per role) | No |
| `list_servers` | Show the full server inventory | No |
| `get_server_status` | Quick health check: uptime, disk, memory | No |
| `health_check` | Comprehensive health: disk, memory, load, containers, services, OOM kills, I/O wait | No |

### Docker & Systemd (4 tools)

| Tool | What It Does | Approval |
|---|---|---|
| `docker_ps` | List running containers on a server | No |
| `docker_logs` | Fetch container logs (with tail/since) | No |
| `service_status` | Check systemd service status | No |
| `service_journal` | Read systemd journal for a service | No |

### Monitoring (1 tool)

| Tool | What It Does | Approval |
|---|---|---|
| `query_metrics` | PromQL query against VictoriaMetrics | No |

### cPanel / WHM (12 tools)

| Tool | What It Does | Approval |
|---|---|---|
| `cpanel_list_accounts` | List cPanel accounts with domain, plan, disk usage | No |
| `cpanel_account_info` | Account details: domain, disk, bandwidth, plan | No |
| `cpanel_ssl_status` | SSL certificates: expiry, coverage, AutoSSL status | No |
| `cpanel_backup_status` | Backup config, last backup time, failures | No |
| `cpanel_email_deliverability` | Email deliverability and reputation | No |
| `cpanel_mail_queue` | Mail queue inspection, bounce detection | No |
| `cpanel_domain_lookup` | Domain ownership and config | No |
| `cpanel_list_domains` | All domains (parked, addons, subdomains) | No |
| `cpanel_suspension_info` | Account suspension status and reason | No |
| `cpanel_disk_quota` | Disk quota usage for an account | No |
| `cpanel_php_version` | PHP version(s) configured | No |
| `cpanel_email_diagnostic` | Deep email diagnostics (SPF, DKIM, DMARC) | No |

### WordPress (12 tools)

| Tool | What It Does | Approval |
|---|---|---|
| `wp_sites` | Find WordPress installs on a server | No |
| `wp_health` | WP-CLI site health: version, DB status, updates | No |
| `wp_plugin_status` | Plugin list with status and updates | No |
| `wp_core_update` | Core version and available updates | No |
| `wp_db_check` | Database integrity and table sizes | No |
| `wp_cron_status` | WP-Cron health: pending events, overdue jobs | No |
| `wp_search_replace_dry` | Dry-run search-replace (safe, read-only) | No |
| `wp_security_scan` | PHP in uploads, modified core, obfuscated code | No |
| `wp_file_integrity` | Core file checksums vs wordpress.org | No |
| `wp_performance` | Object cache, page cache, autoload bloat, PHP limits | No |
| `wp_cleanup_preview` | Preview what cleanup would remove (read-only) | No |
| `wp_scan_all` | Batch scan ALL WordPress installs on a server | No |

### Web Server & SSL (6 tools)

| Tool | What It Does | Approval |
|---|---|---|
| `ssl_cert_check` | SSL certificate details: issuer, expiry, chain | No |
| `apache_status` | Apache status: connections, vhost config, syntax check | No |
| `web_error_log` | Parse error logs, group by type | No |
| `dns_check` | DNS resolution and record verification | No |
| `access_log_analysis` | Traffic patterns, status codes, user agents | No |
| `mod_security_log` | ModSecurity WAF logs: rules, false positives | No |

### MySQL / MariaDB (7 tools)

| Tool | What It Does | Approval |
|---|---|---|
| `mysql_status` | Uptime, connections, threads, queries/sec | No |
| `mysql_processlist` | Active queries, flags long-running ones | No |
| `mysql_slow_queries` | Slow query analysis | No |
| `mysql_database_sizes` | Database disk usage and row counts | No |
| `mysql_table_check` | Table integrity check | No |
| `mysql_table_repair` | Repair corrupted tables | **Yes** |
| `mysql_table_optimize` | Optimize table fragmentation | **Yes** |

### Pterodactyl Game Servers (6 tools)

| Tool | What It Does | Approval |
|---|---|---|
| `pterodactyl_list_servers` | List game servers with status, node, owner, resources | No |
| `pterodactyl_server_status` | Resource usage (CPU, RAM, disk) for a game server | No |
| `pterodactyl_power_action` | Start, stop, restart, kill a game server | **Yes** |
| `pterodactyl_console_command` | Send console command to a running game server (RCON) | Allowlisted |
| `pterodactyl_overview` | Cross-node dashboard: all Wings nodes, containers, resources | No |
| `mod_conflict_check` | Detect plugin/mod conflicts (Minecraft, Rust, CS2, Valheim, ARK) | No |

### Deep Diagnostics (7 tools)

| Tool | What It Does | Approval |
|---|---|---|
| `diagnose_site` | Full site diagnosis from a domain name (DNS, SSL, HTTP, WP, PHP, DB) | No |
| `game_server_diagnose` | Deep game server lag analysis (CPU throttle, I/O, memory, GC, network) | No |
| `wp_deep_performance` | Deep WP performance: TTFB, OPcache, autoload bloat, image sizes | No |
| `wp_elementor_diagnose` | **NEW** — Elementor HTML/JS bleed-out diagnostics (smart quotes, corrupted data, plugin conflicts, JS syntax) | No |
| `page_debug` | Page rendering issues: unclosed tags, PHP errors, mixed content | No |
| `security_audit` | SSH config, open ports, firewall, SUID binaries, failed logins | No |
| `cron_audit` | All cron jobs across users, overlapping schedules | No |

### Incident Response & Investigation (8 tools)

| Tool | What It Does | Approval |
|---|---|---|
| `what_changed` | Detect recent changes: packages, Docker pulls, config mods, restarts | No |
| `incident_timeline` | Chronological timeline from logs, dmesg, Docker events | No |
| `blast_radius` | Preview impact of a destructive action before executing | No |
| `infrastructure_pulse` | Fast health snapshot of ALL servers | No |
| `log_correlate` | Cross-server log correlation within a time window | No |
| `uptime_probe` | HTTP endpoint probe: response time, SSL, content match | No |
| `ticket_intake` | Classify a customer complaint and generate a diagnostic plan | No |
| `incident_report` | Generate structured post-incident report | No |

### Client Communication (2 tools)

| Tool | What It Does | Approval |
|---|---|---|
| `explain_to_client` | Generate non-technical client-friendly explanation | No |
| `shift_handoff` | Generate shift handoff summary | No |

### Configuration & Compliance (5 tools)

| Tool | What It Does | Approval |
|---|---|---|
| `config_diff` | Compare configs between servers to detect drift | No |
| `config_baseline` | Check configs against security baselines | No |
| `backup_audit` | Backup status across cPanel, Pterodactyl, MySQL | No |
| `customer_impact` | Map infra issues to customer-facing impact | No |
| `resource_rightsizing` | Identify over/under-provisioned resources | No |

---

## Security Model

This is the part that lets you sleep at night.

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

Shell metacharacters are **rejected, not escaped** (escaping is a game you eventually lose). The following are blocked in all command arguments:

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
model: claude-sonnet-4-5-20250929    # Claude model to use
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

## Project Structure

```
bastion-agent/
├── install.sh                       # One-command installer
├── pyproject.toml                   # Package metadata & dependencies
├── requirements.txt                 # Pinned dependency versions
│
├── agent/                           # Main Python package
│   ├── __init__.py                  # Version
│   ├── main.py                      # CLI entry point (Click), tool registration
│   ├── client.py                    # Anthropic API + conversation loop
│   ├── config.py                    # Pydantic config models + YAML loader
│   ├── inventory.py                 # Server inventory model
│   ├── prompts.py                   # Dynamic system prompt builder
│   ├── tools/
│   │   ├── base.py                  # BaseTool protocol + ToolResult
│   │   ├── registry.py             # Registration, schema gen, secure dispatch
│   │   ├── local.py                 # run_local_command
│   │   ├── remote.py               # run_remote_command (asyncssh + pool)
│   │   ├── files.py                 # read_file (local + remote)
│   │   ├── server_info.py          # list_servers, get_server_status, health_check
│   │   ├── docker_tools.py         # docker_ps, docker_logs, SSH pool
│   │   ├── systemd.py              # service_status, service_journal
│   │   ├── monitoring.py           # query_metrics (VictoriaMetrics)
│   │   ├── cpanel.py               # 12 cPanel/WHM tools
│   │   ├── wordpress.py            # 11 WordPress/WP-CLI tools
│   │   ├── webtools.py             # SSL, Apache, DNS, access logs, ModSec
│   │   ├── mysql_tools.py          # MySQL status, processlist, repair, optimize
│   │   ├── pterodactyl.py          # Panel API: list, status, power, console
│   │   ├── pterodactyl_overview.py # Cross-node Pterodactyl dashboard
│   │   ├── diagnose_site.py        # Full site diagnosis from domain name
│   │   ├── game_diagnose.py        # Deep game server lag diagnostics
│   │   ├── wp_scan.py              # Batch WordPress security scanner
│   │   ├── wp_deep_scan.py         # Deep WP performance (TTFB, OPcache, etc.)
│   │   ├── wp_elementor_diagnose.py # Elementor HTML/JS bleed-out diagnostics
│   │   ├── page_debug.py           # Page rendering issue detection
│   │   ├── security_tools.py       # Security audit, cron audit
│   │   ├── log_tools.py            # Log correlation, uptime probe
│   │   ├── incident_tools.py       # what_changed, incident_timeline, blast_radius
│   │   ├── pulse.py                # infrastructure_pulse
│   │   ├── communication.py        # explain_to_client, shift_handoff
│   │   ├── ticket_intake.py        # Customer ticket classification
│   │   ├── incident_report.py      # Post-incident report generation
│   │   ├── config_tools.py         # Config diff, baseline checking
│   │   ├── backup_audit.py         # Backup status auditing
│   │   ├── customer_impact.py      # Customer impact mapping
│   │   ├── resource_rightsizing.py  # Resource usage analysis
│   │   └── mod_conflict_check.py   # Game server mod/plugin conflict detection
│   ├── security/
│   │   ├── allowlist.py             # Command pattern matching engine
│   │   ├── approval.py             # Human-in-the-loop gate
│   │   ├── audit.py                 # Structured JSON audit logger
│   │   ├── sanitizer.py            # Shell injection prevention
│   │   └── console_allowlist.py    # Pterodactyl console command allowlist
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
├── tests/                           # 660+ tests
│   ├── conftest.py
│   ├── test_sanitizer.py
│   ├── test_allowlist.py
│   ├── test_approval.py
│   ├── test_audit.py
│   ├── test_inventory.py
│   ├── test_tools.py
│   ├── test_elementor_diagnose.py
│   ├── test_pterodactyl_overview.py
│   ├── test_customer_impact.py
│   ├── test_resource_rightsizing.py
│   ├── test_mod_conflict_check.py
│   ├── test_incident_report.py
│   └── ...                          # + more
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
pytest                    # all tests
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

## Updating via FTP

If your bastion server doesn't have git access (or you prefer FTP for deployments), you can update the agent files manually.

### What to Upload

The agent code lives entirely in the `agent/` directory. Config files are separate.

```
Upload these:                     To this path on the server:
─────────────────                 ──────────────────────────
agent/                        →   /opt/bastion-agent/agent/
tests/                        →   /opt/bastion-agent/tests/       (optional, for verification)
pyproject.toml                →   /opt/bastion-agent/pyproject.toml
requirements.txt              →   /opt/bastion-agent/requirements.txt
```

**Do NOT upload** `config/` — your live config files live in `/etc/bastion-agent/` and should not be overwritten.

### Step-by-Step FTP Update

1. **Connect via FTP/SFTP** to the bastion server (SFTP recommended for security):
   ```
   Host: your-bastion-ip
   User: root (or a sudo user)
   Path: /opt/bastion-agent/
   ```

2. **Stop the agent** before uploading:
   ```bash
   sudo systemctl stop bastion-agent
   ```

3. **Upload the updated files** — replace the entire `agent/` directory:
   - Delete `/opt/bastion-agent/agent/` on the server
   - Upload the new `agent/` directory
   - Upload `pyproject.toml` and `requirements.txt` if dependencies changed

4. **Install any new dependencies** (only needed if `requirements.txt` changed):
   ```bash
   sudo /opt/bastion-agent/venv/bin/pip install -r /opt/bastion-agent/requirements.txt
   ```

5. **Reinstall the package** (picks up new tool registrations):
   ```bash
   cd /opt/bastion-agent && sudo /opt/bastion-agent/venv/bin/pip install -e .
   ```

6. **Restart the agent**:
   ```bash
   sudo systemctl start bastion-agent
   ```

7. **Verify it started**:
   ```bash
   sudo systemctl status bastion-agent
   sudo journalctl -u bastion-agent -n 20 --no-pager
   ```

### Quick One-Liner (if you have SSH)

If you can SSH but not git pull:

```bash
# From your local machine with the updated repo:
rsync -avz --exclude='.git' --exclude='config/' --exclude='logs/' --exclude='__pycache__' \
  ./agent/ ./tests/ ./pyproject.toml ./requirements.txt \
  root@bastion-ip:/opt/bastion-agent/

# Then on the bastion:
ssh root@bastion-ip "systemctl restart bastion-agent && systemctl status bastion-agent"
```

### Updating Config (Adding New Servers)

Config files live in `/etc/bastion-agent/`, not in the code directory. To add a new server:

```bash
# Edit the server inventory
sudo nano /etc/bastion-agent/servers.yaml

# Edit permissions if you need a new role
sudo nano /etc/bastion-agent/permissions.yaml

# Restart to pick up changes
sudo systemctl restart bastion-agent
```

---

## Recent Changes

### March 2026 — Major Feature Additions

**Elementor Diagnostics** (`wp_elementor_diagnose`)
- Diagnoses HTML/JS bleed-out in Elementor pages
- Detects smart/curly quotes pasted from Word/email (the #1 cause of `Uncaught SyntaxError`)
- Checks Elementor data integrity (corrupted `_elementor_data` JSON in postmeta)
- Scans for unclosed `<script>`, `<style>`, `<iframe>` tags in HTML widgets
- Detects unclosed template literals (backticks), unbalanced braces, control characters
- Identifies 15 known plugin conflicts (Autoptimize, WP Rocket, competing page builders, etc.)
- Checks CSS print method, Elementor version mismatches (core vs Pro), missing frontend JS
- Generates actionable fix recommendations

**Pterodactyl Console Command Allowlist** (`console_allowlist.py`)
- Game-aware command filtering for Pterodactyl console commands
- Auto-detects game type (Minecraft, Rust, CS2, Valheim, ARK, Terraria)
- Read-only commands (e.g. `list`, `tps`, `status`) execute without operator approval
- Dangerous commands (`stop`, `op`, `rcon.login`, `oxide.grant`) require approval
- Blocks server-stopping commands entirely (use `pterodactyl_power` instead)

**8 New Operational Tools:**
- `pterodactyl_overview` — Cross-node Pterodactyl dashboard (all Wings nodes at once)
- `customer_impact` — Map infra issues to customer-facing impact (accounts, domains, revenue)
- `resource_rightsizing` — Find over/under-provisioned resources across servers
- `mod_conflict_check` — Detect game server mod/plugin conflicts and incompatibilities
- `incident_report` — Generate structured post-incident reports
- `backup_audit` — Audit backup status across cPanel, Pterodactyl, MySQL
- `config_diff` / `config_baseline` — Configuration drift detection and compliance checking

**Previous Additions (this month):**
- `infrastructure_pulse` — Fast health snapshot of all servers in one call
- `blast_radius` — Preview impact of destructive actions before executing
- `incident_timeline` — Build chronological incident timelines from logs
- `what_changed` — Detect recent changes on a server (packages, configs, restarts)
- `diagnose_site` — Full site diagnosis from just a domain name
- `game_server_diagnose` — Deep game server lag analysis
- `wp_deep_performance` — Deep WordPress performance (TTFB, OPcache, autoload)
- `page_debug` — Web page rendering issue detection
- `security_audit` / `cron_audit` — Server security and cron auditing
- `ticket_intake` — Customer complaint classification
- `explain_to_client` / `shift_handoff` — Client communication helpers
- SSH connection pooling for faster multi-command operations
- 660+ tests covering all tools

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
