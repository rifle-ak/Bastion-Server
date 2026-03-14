"""Customer impact assessment tool.

Maps infrastructure issues to customer-facing impact by querying
affected accounts, domains, databases, and game servers. Produces
a structured report with impact summary, revenue risk, and recovery
priorities.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from agent.inventory import Inventory
from agent.tools.base import BaseTool, ToolResult
from agent.tools.docker_tools import _run_on_server


# ---------------------------------------------------------------------------
# Service definitions and data collection commands
# ---------------------------------------------------------------------------

VALID_SERVICES = (
    "apache",
    "mysql",
    "nginx",
    "wings",
    "docker",
    "server",
    "php-fpm",
    "exim",
    "dovecot",
    "named",
)

# Commands keyed by logical data point.  Each returns a single ToolResult.
_CPANEL_ACCOUNT_LIST_CMD = "ls /var/cpanel/users/"
_CPANEL_ACCOUNT_COUNT_CMD = "ls /var/cpanel/users/ | wc -l"
_CPANEL_USERDOMAINS_CMD = "cat /etc/userdomains"
_CPANEL_TRAFFIC_CMD = "wc -l /home/*/logs/*-$(date +%b-%Y) 2>/dev/null | tail -1"
_MYSQL_DB_COUNT_CMD = (
    "mysql -N -e \"SELECT COUNT(*) FROM information_schema.schemata "
    "WHERE schema_name NOT IN "
    "('information_schema','mysql','performance_schema','sys')\""
)
_MYSQL_DB_USERS_CMD = (
    "mysql -N -e \"SELECT COUNT(DISTINCT User) FROM mysql.user "
    "WHERE User NOT IN ('root','mysql.sys','mysql.session','mysql.infoschema','debian-sys-maint')\""
)
_DOCKER_PS_CMD = "docker ps --format '{{.Names}}'"
_DOCKER_COUNT_CMD = "docker ps -q | wc -l"
_WINGS_SERVERS_CMD = "docker ps --filter 'label=Service=Pterodactyl' --format '{{.Names}}'"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ImpactData:
    """Raw data collected from the server for impact analysis."""

    account_count: int = 0
    accounts: list[str] | None = None
    domain_map: dict[str, str] | None = None
    domain_count: int = 0
    database_count: int = 0
    database_user_count: int = 0
    container_count: int = 0
    container_names: list[str] | None = None
    traffic_estimate: str = ""
    errors: list[str] | None = None


# ---------------------------------------------------------------------------
# Service-to-dependency mapping
# ---------------------------------------------------------------------------

_SERVICE_DEPENDENCIES: dict[str, list[str]] = {
    "apache": ["All websites on this server"],
    "nginx": ["All websites on this server (reverse proxy / web server)"],
    "php-fpm": ["All PHP-based websites on this server"],
    "mysql": ["All websites using databases", "All applications with MySQL backends"],
    "wings": ["All Pterodactyl game servers on this node"],
    "docker": ["All containerized services", "All Pterodactyl game servers if Wings uses Docker"],
    "exim": ["All outbound/inbound email for hosted accounts"],
    "dovecot": ["All IMAP/POP3 email access for hosted accounts"],
    "named": ["DNS resolution for all domains hosted on this server"],
    "server": [
        "All websites",
        "All email",
        "All databases",
        "All game servers",
        "Complete outage of all services",
    ],
}


# ---------------------------------------------------------------------------
# Impact report builder (standalone for testability)
# ---------------------------------------------------------------------------

def _build_impact_report(
    server: str,
    service: str,
    data: ImpactData,
) -> str:
    """Build a human-readable impact report from collected data.

    This is a pure function (no I/O) so it can be unit-tested
    independently of SSH connectivity.
    """
    lines: list[str] = []

    # --- Impact Summary ---
    lines.append("## Impact Summary")
    summary_parts: list[str] = []
    if data.account_count > 0:
        summary_parts.append(f"{data.account_count} customer account(s)")
    if data.domain_count > 0:
        summary_parts.append(f"{data.domain_count} website(s)/domain(s)")
    if data.database_count > 0:
        summary_parts.append(f"{data.database_count} database(s)")
    if data.container_count > 0:
        summary_parts.append(f"{data.container_count} container(s)/game server(s)")
    if summary_parts:
        lines.append(f"- **Affected**: {', '.join(summary_parts)}")
    else:
        lines.append("- **Affected**: Unable to determine exact count (see errors below)")
    lines.append(f"- **Service down**: {service}")
    lines.append(f"- **Server**: {server}")
    lines.append("")

    # --- Service Dependencies ---
    deps = _SERVICE_DEPENDENCIES.get(service, [])
    if deps:
        lines.append("## Service Dependencies")
        for dep in deps:
            lines.append(f"- {dep}")
        lines.append("")

    # --- Affected Accounts ---
    if data.accounts:
        lines.append("## Affected Accounts")
        if data.domain_map:
            # Group domains by account
            account_domains: dict[str, list[str]] = {}
            for domain, account in data.domain_map.items():
                account_domains.setdefault(account, []).append(domain)
            for account in data.accounts:
                domains = account_domains.get(account, [])
                if domains:
                    lines.append(f"- **{account}**: {len(domains)} domain(s)")
                    for domain in domains[:10]:  # Cap display at 10 per account
                        lines.append(f"  - {domain}")
                    if len(domains) > 10:
                        lines.append(f"  - ... and {len(domains) - 10} more")
                else:
                    lines.append(f"- **{account}**: domain count unknown")
        else:
            for account in data.accounts[:50]:  # Cap display at 50
                lines.append(f"- {account}")
            if len(data.accounts) > 50:
                lines.append(f"- ... and {len(data.accounts) - 50} more")
        lines.append("")

    # --- Container / Game Server List ---
    if data.container_names:
        lines.append("## Affected Containers / Game Servers")
        for name in data.container_names[:30]:
            lines.append(f"- {name}")
        if len(data.container_names) > 30:
            lines.append(f"- ... and {len(data.container_names) - 30} more")
        lines.append("")

    # --- Traffic Estimate ---
    if data.traffic_estimate:
        lines.append("## Traffic Estimate")
        lines.append(f"- Log line count (current month): {data.traffic_estimate}")
        lines.append("")

    # --- Revenue Risk ---
    lines.append("## Revenue Risk")
    risk = _assess_revenue_risk(service, data)
    lines.append(f"- **Level**: {risk}")
    lines.append(f"- **Reasoning**: {_risk_reasoning(service, data, risk)}")
    lines.append("")

    # --- Recommended Communication ---
    lines.append("## Recommended Communication")
    lines.extend(_communication_recommendations(service, data))
    lines.append("")

    # --- Recovery Priority ---
    lines.append("## Recovery Priority")
    lines.extend(_recovery_priority(service))
    lines.append("")

    # --- Errors ---
    if data.errors:
        lines.append("## Data Collection Notes")
        for err in data.errors:
            lines.append(f"- {err}")
        lines.append("")

    return "\n".join(lines)


def _assess_revenue_risk(service: str, data: ImpactData) -> str:
    """Classify revenue risk as HIGH, MEDIUM, or LOW."""
    # Full server outage is always high
    if service == "server":
        return "HIGH"

    # Many accounts or many containers -> high
    if data.account_count >= 20 or data.container_count >= 10:
        return "HIGH"

    # Moderate counts
    if data.account_count >= 5 or data.container_count >= 3:
        return "MEDIUM"

    # Core customer-facing services default to medium even with low counts
    if service in ("apache", "nginx", "mysql", "wings"):
        return "MEDIUM"

    return "LOW"


def _risk_reasoning(service: str, data: ImpactData, risk: str) -> str:
    """Provide human-readable reasoning for the risk level."""
    if service == "server":
        return "Complete server outage affects all hosted services and customers."

    parts: list[str] = []
    if data.account_count > 0:
        parts.append(f"{data.account_count} customer account(s) affected")
    if data.domain_count > 0:
        parts.append(f"{data.domain_count} domain(s) potentially unreachable")
    if data.container_count > 0:
        parts.append(f"{data.container_count} game server(s) offline")
    if data.database_count > 0:
        parts.append(f"{data.database_count} database(s) inaccessible")

    if not parts:
        return f"{service} service disruption with undetermined scope."

    return "; ".join(parts) + "."


def _communication_recommendations(service: str, data: ImpactData) -> list[str]:
    """Generate communication recommendations."""
    recs: list[str] = []
    if data.account_count > 0:
        recs.append(
            f"- Notify {data.account_count} affected customer(s) via support ticket or email."
        )
    if service in ("apache", "nginx", "php-fpm", "mysql"):
        recs.append("- Post status page update for web hosting service disruption.")
    if service in ("wings", "docker"):
        recs.append("- Post status page update for game server service disruption.")
    if service in ("exim", "dovecot"):
        recs.append("- Notify customers of email service interruption.")
    if service == "named":
        recs.append("- Notify customers of DNS resolution issues; advise checking propagation.")
    if service == "server":
        recs.append("- Post status page update for full server outage.")
        recs.append("- Open priority support tickets for all affected customers.")
    if not recs:
        recs.append(f"- Assess whether {service} outage is customer-visible before notifying.")
    recs.append("- Include estimated time to resolution if known.")
    return recs


def _recovery_priority(service: str) -> list[str]:
    """Suggest service restoration order."""
    priorities: dict[str, list[str]] = {
        "server": [
            "1. Restore network connectivity / boot server",
            "2. Verify filesystem integrity",
            "3. Start MySQL/MariaDB (database layer)",
            "4. Start Apache/Nginx (web layer)",
            "5. Start PHP-FPM",
            "6. Start mail services (Exim, Dovecot)",
            "7. Start DNS (named)",
            "8. Start Docker / Pterodactyl Wings",
            "9. Verify all customer sites and services are responding",
        ],
        "apache": [
            "1. Check Apache error logs: tail -100 /var/log/apache2/error_log",
            "2. Validate configuration: apachectl configtest",
            "3. Restart Apache: systemctl restart httpd (or apache2)",
            "4. Verify sites are responding",
        ],
        "nginx": [
            "1. Check Nginx error logs: tail -100 /var/log/nginx/error.log",
            "2. Validate configuration: nginx -t",
            "3. Restart Nginx: systemctl restart nginx",
            "4. Verify sites are responding",
        ],
        "mysql": [
            "1. Check MySQL error log: tail -100 /var/log/mysql/error.log",
            "2. Check disk space: df -h (MySQL often fails due to full disk)",
            "3. Restart MySQL: systemctl restart mysql (or mariadb)",
            "4. Verify database connectivity",
            "5. Check for crashed tables: mysqlcheck --all-databases",
        ],
        "php-fpm": [
            "1. Check PHP-FPM logs",
            "2. Restart PHP-FPM: systemctl restart php-fpm (or php*-fpm)",
            "3. Verify PHP sites are loading",
        ],
        "wings": [
            "1. Check Wings logs: journalctl -u wings --no-pager -n 100",
            "2. Verify Docker daemon is running",
            "3. Restart Wings: systemctl restart wings",
            "4. Verify game server containers are starting",
        ],
        "docker": [
            "1. Check Docker daemon logs: journalctl -u docker --no-pager -n 100",
            "2. Check disk space (Docker can exhaust storage)",
            "3. Restart Docker: systemctl restart docker",
            "4. Verify containers are running: docker ps",
        ],
        "exim": [
            "1. Check Exim logs: tail -100 /var/log/exim_mainlog",
            "2. Check mail queue: exim -bpc",
            "3. Restart Exim: systemctl restart exim",
        ],
        "dovecot": [
            "1. Check Dovecot logs: tail -100 /var/log/dovecot.log",
            "2. Restart Dovecot: systemctl restart dovecot",
            "3. Verify IMAP/POP3 connectivity",
        ],
        "named": [
            "1. Check named logs: journalctl -u named --no-pager -n 100",
            "2. Validate zone files: named-checkconf",
            "3. Restart named: systemctl restart named",
            "4. Verify DNS resolution from external source",
        ],
    }
    return priorities.get(service, [f"1. Investigate {service} failure and restore service."])


# ---------------------------------------------------------------------------
# Data collection helpers
# ---------------------------------------------------------------------------

async def _collect_cpanel_data(
    inventory: Inventory,
    server: str,
    include_domains: bool,
) -> ImpactData:
    """Collect cPanel hosting data: accounts, domains, traffic."""
    data = ImpactData(errors=[])

    # Run account list and count in parallel
    coros = [
        _run_on_server(inventory, server, _CPANEL_ACCOUNT_LIST_CMD),
        _run_on_server(inventory, server, _CPANEL_TRAFFIC_CMD),
    ]
    if include_domains:
        coros.append(_run_on_server(inventory, server, _CPANEL_USERDOMAINS_CMD))

    results = await asyncio.gather(*coros, return_exceptions=True)

    # Parse account list
    acct_result = results[0]
    if isinstance(acct_result, Exception):
        data.errors.append(f"Account list failed: {acct_result}")
    elif isinstance(acct_result, ToolResult) and acct_result.success:
        accounts = [
            a.strip() for a in acct_result.output.splitlines() if a.strip()
        ]
        data.accounts = accounts
        data.account_count = len(accounts)
    elif isinstance(acct_result, ToolResult):
        data.errors.append(f"Account list error: {acct_result.error}")

    # Parse traffic estimate
    traffic_result = results[1]
    if isinstance(traffic_result, ToolResult) and traffic_result.success:
        data.traffic_estimate = traffic_result.output.strip()
    elif isinstance(traffic_result, Exception):
        data.errors.append(f"Traffic estimate failed: {traffic_result}")

    # Parse domain map
    if include_domains and len(results) > 2:
        domain_result = results[2]
        if isinstance(domain_result, ToolResult) and domain_result.success:
            domain_map: dict[str, str] = {}
            for line in domain_result.output.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                parts = line.split(":", 1)
                if len(parts) == 2:
                    domain = parts[0].strip()
                    account = parts[1].strip()
                    if domain and account:
                        domain_map[domain] = account
            data.domain_map = domain_map
            data.domain_count = len(domain_map)
        elif isinstance(domain_result, Exception):
            data.errors.append(f"Domain list failed: {domain_result}")
        elif isinstance(domain_result, ToolResult):
            data.errors.append(f"Domain list error: {domain_result.error}")

    return data


async def _collect_mysql_data(inventory: Inventory, server: str) -> ImpactData:
    """Collect MySQL/MariaDB impact data: database and user counts."""
    data = ImpactData(errors=[])

    results = await asyncio.gather(
        _run_on_server(inventory, server, _MYSQL_DB_COUNT_CMD),
        _run_on_server(inventory, server, _MYSQL_DB_USERS_CMD),
        return_exceptions=True,
    )

    db_result = results[0]
    if isinstance(db_result, ToolResult) and db_result.success:
        try:
            data.database_count = int(db_result.output.strip())
        except ValueError:
            data.errors.append(f"Could not parse database count: {db_result.output}")
    elif isinstance(db_result, Exception):
        data.errors.append(f"Database count failed: {db_result}")
    elif isinstance(db_result, ToolResult):
        data.errors.append(f"Database count error: {db_result.error}")

    user_result = results[1]
    if isinstance(user_result, ToolResult) and user_result.success:
        try:
            data.database_user_count = int(user_result.output.strip())
        except ValueError:
            data.errors.append(f"Could not parse DB user count: {user_result.output}")
    elif isinstance(user_result, Exception):
        data.errors.append(f"DB user count failed: {user_result}")

    return data


async def _collect_container_data(inventory: Inventory, server: str) -> ImpactData:
    """Collect Docker/Wings container data."""
    data = ImpactData(errors=[])

    results = await asyncio.gather(
        _run_on_server(inventory, server, _DOCKER_PS_CMD),
        _run_on_server(inventory, server, _DOCKER_COUNT_CMD),
        return_exceptions=True,
    )

    names_result = results[0]
    if isinstance(names_result, ToolResult) and names_result.success:
        names = [n.strip() for n in names_result.output.splitlines() if n.strip()]
        data.container_names = names
    elif isinstance(names_result, Exception):
        data.errors.append(f"Container list failed: {names_result}")
    elif isinstance(names_result, ToolResult):
        data.errors.append(f"Container list error: {names_result.error}")

    count_result = results[1]
    if isinstance(count_result, ToolResult) and count_result.success:
        try:
            data.container_count = int(count_result.output.strip())
        except ValueError:
            data.errors.append(f"Could not parse container count: {count_result.output}")
    elif isinstance(count_result, Exception):
        data.errors.append(f"Container count failed: {count_result}")

    return data


def _merge_impact_data(base: ImpactData, *others: ImpactData) -> ImpactData:
    """Merge multiple ImpactData instances into one."""
    merged = ImpactData(
        account_count=base.account_count,
        accounts=list(base.accounts) if base.accounts else None,
        domain_map=dict(base.domain_map) if base.domain_map else None,
        domain_count=base.domain_count,
        database_count=base.database_count,
        database_user_count=base.database_user_count,
        container_count=base.container_count,
        container_names=list(base.container_names) if base.container_names else None,
        traffic_estimate=base.traffic_estimate,
        errors=list(base.errors) if base.errors else [],
    )
    for other in others:
        if other.account_count > merged.account_count:
            merged.account_count = other.account_count
        if other.accounts:
            if merged.accounts is None:
                merged.accounts = []
            merged.accounts.extend(other.accounts)
        if other.domain_map:
            if merged.domain_map is None:
                merged.domain_map = {}
            merged.domain_map.update(other.domain_map)
        merged.domain_count = max(merged.domain_count, other.domain_count)
        if other.database_count > merged.database_count:
            merged.database_count = other.database_count
        if other.database_user_count > merged.database_user_count:
            merged.database_user_count = other.database_user_count
        if other.container_count > merged.container_count:
            merged.container_count = other.container_count
        if other.container_names:
            if merged.container_names is None:
                merged.container_names = []
            merged.container_names.extend(other.container_names)
        if other.traffic_estimate and not merged.traffic_estimate:
            merged.traffic_estimate = other.traffic_estimate
        if other.errors:
            if merged.errors is None:
                merged.errors = []
            merged.errors.extend(other.errors)

    return merged


# ---------------------------------------------------------------------------
# Main tool class
# ---------------------------------------------------------------------------

class CustomerImpact(BaseTool):
    """Assess customer impact of an infrastructure service outage."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "customer_impact"

    @property
    def description(self) -> str:
        return (
            "Assess customer impact when an infrastructure service goes down. "
            "Maps a service outage on a specific server to affected customers, "
            "websites, game servers, and databases. Returns an impact report "
            "with revenue risk and recovery priorities."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server name from the inventory.",
                },
                "affected_service": {
                    "type": "string",
                    "description": (
                        "The service that is down or degraded. "
                        "One of: apache, mysql, nginx, wings, docker, "
                        "server, php-fpm, exim, dovecot, named."
                    ),
                    "enum": list(VALID_SERVICES),
                },
                "include_domains": {
                    "type": "boolean",
                    "description": (
                        "Include per-account domain listing in the report "
                        "(default true). Set to false for faster results."
                    ),
                    "default": True,
                },
            },
            "required": ["server", "affected_service"],
        }

    async def execute(
        self,
        *,
        server: str,
        affected_service: str,
        include_domains: bool = True,
        **kwargs: Any,
    ) -> ToolResult:
        """Collect data and build a customer impact report."""
        # Validate server exists in inventory
        try:
            self._inventory.get_server(server)
        except KeyError as e:
            return ToolResult(error=str(e), exit_code=1)

        # Validate service name
        if affected_service not in VALID_SERVICES:
            return ToolResult(
                error=(
                    f"Unknown service: {affected_service!r}. "
                    f"Valid services: {', '.join(VALID_SERVICES)}"
                ),
                exit_code=1,
            )

        # Decide which data to collect based on the affected service
        data = await self._collect_data(server, affected_service, include_domains)

        report = _build_impact_report(server, affected_service, data)
        return ToolResult(output=report, exit_code=0)

    async def _collect_data(
        self,
        server: str,
        service: str,
        include_domains: bool,
    ) -> ImpactData:
        """Collect relevant data based on the affected service."""
        collectors: list[Any] = []

        # cPanel data for web/email/DNS/server-level services
        if service in ("apache", "nginx", "php-fpm", "exim", "dovecot", "named", "server"):
            collectors.append(
                _collect_cpanel_data(self._inventory, server, include_domains)
            )

        # MySQL data for database or server-level outages
        if service in ("mysql", "server"):
            collectors.append(_collect_mysql_data(self._inventory, server))

        # Container data for Docker/Wings or server-level outages
        if service in ("wings", "docker", "server"):
            collectors.append(_collect_container_data(self._inventory, server))

        if not collectors:
            return ImpactData(errors=[f"No data collectors for service: {service}"])

        results = await asyncio.gather(*collectors, return_exceptions=True)

        # Merge all collected data
        data_parts: list[ImpactData] = []
        errors: list[str] = []
        for result in results:
            if isinstance(result, Exception):
                errors.append(f"Data collection error: {result}")
            else:
                data_parts.append(result)

        if not data_parts:
            return ImpactData(errors=errors)

        merged = _merge_impact_data(data_parts[0], *data_parts[1:])
        if errors:
            if merged.errors is None:
                merged.errors = []
            merged.errors.extend(errors)

        return merged
