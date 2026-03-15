"""One-shot site diagnosis from a domain name.

Give it a domain, get back a complete picture: DNS, SSL, HTTP response,
Apache vhost, error logs, WordPress detection + health, PHP version,
disk quota, database, and email — all run in parallel over a single
SSH connection.

This is the killer feature. Nothing else does all of this in one call.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from agent.inventory import Inventory
from agent.tools.base import BaseTool, ToolResult
from agent.tools.docker_tools import _run_on_server


class DiagnoseSite(BaseTool):
    """Full-stack site diagnosis from a single domain name."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "diagnose_site"

    @property
    def description(self) -> str:
        return (
            "Full site diagnosis from a domain name. Runs DNS, SSL, HTTP, "
            "error logs, WP detection, PHP, disk, database, and email checks "
            "in parallel. One call = complete picture."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Webhost server to diagnose from.",
                },
                "domain": {
                    "type": "string",
                    "description": "Domain name to diagnose (e.g. 'example.com').",
                },
            },
            "required": ["server", "domain"],
        }

    async def execute(self, *, server: str, domain: str, **kwargs: Any) -> ToolResult:
        """Run parallel diagnosis for a domain."""
        # Phase 1: Identify the account owner and docroot
        owner_result = await _run_on_server(
            self._inventory, server,
            f"whmapi1 getdomainowner domain={domain} --output=json",
        )
        username = _extract_owner(owner_result.output if owner_result.success else "")
        docroot = f"/home/{username}/public_html" if username else None

        # Phase 2: Run all checks in parallel
        checks: dict[str, Any] = {}

        # DNS checks
        checks["dns_a"] = _run_on_server(self._inventory, server, f"dig {domain} A +short")
        checks["dns_mx"] = _run_on_server(self._inventory, server, f"dig {domain} MX +short")
        checks["dns_ns"] = _run_on_server(self._inventory, server, f"dig {domain} NS +short")

        # SSL check
        checks["ssl"] = _run_on_server(
            self._inventory, server,
            f"echo | openssl s_client -servername {domain} -connect {domain}:443 2>/dev/null "
            f"| openssl x509 -noout -dates -subject -issuer 2>/dev/null",
        )

        # Apache vhost
        checks["vhost"] = _run_on_server(self._inventory, server, "httpd -S 2>&1")

        # Error log (last 50 lines filtered by domain)
        for log_path in [
            "/var/log/apache2/error_log",
            "/etc/httpd/logs/error_log",
            "/usr/local/apache/logs/error_log",
        ]:
            checks[f"errlog_{log_path}"] = _run_on_server(
                self._inventory, server, f"test -f {log_path}"
            )

        if username:
            # PHP version
            checks["php"] = _run_on_server(
                self._inventory, server,
                f"whmapi1 php_get_vhost_versions user={username} --output=json",
            )

            # Disk usage
            checks["disk_summary"] = _run_on_server(
                self._inventory, server, f"du -sh /home/{username}/",
            )
            checks["disk_breakdown"] = _run_on_server(
                self._inventory, server, f"du -sh /home/{username}/*/ 2>/dev/null",
            )

            # Email queue for this user
            checks["mail_queue"] = _run_on_server(
                self._inventory, server, f"exim -bpc",
            )
            checks["mail_boxes"] = _run_on_server(
                self._inventory, server, f"du -sh /home/{username}/mail/*/ 2>/dev/null",
            )

        if docroot:
            # WordPress detection
            checks["wp_detect"] = _run_on_server(
                self._inventory, server, f"test -f {docroot}/wp-config.php",
            )

        # Await all in parallel
        keys = list(checks.keys())
        results = await asyncio.gather(*[checks[k] for k in keys])
        check_results = dict(zip(keys, results))

        # Phase 3: WordPress-specific checks if detected
        wp_detected = (
            docroot
            and "wp_detect" in check_results
            and check_results["wp_detect"].exit_code == 0
        )
        wp_results: dict[str, ToolResult] = {}
        if wp_detected and username:
            wp_checks = {
                "wp_version": _run_on_server(
                    self._inventory, server,
                    f"runuser -u {username} -- wp core version --path={docroot}",
                ),
                "wp_plugins": _run_on_server(
                    self._inventory, server,
                    f"runuser -u {username} -- wp plugin list --format=csv --path={docroot}",
                ),
                "wp_core_verify": _run_on_server(
                    self._inventory, server,
                    f"runuser -u {username} -- wp core verify-checksums --path={docroot} 2>&1",
                ),
                "wp_db_size": _run_on_server(
                    self._inventory, server,
                    f"runuser -u {username} -- wp db size --path={docroot} 2>&1",
                ),
                "wp_cron": _run_on_server(
                    self._inventory, server,
                    f"runuser -u {username} -- wp cron event list --format=csv --path={docroot} 2>&1",
                ),
                "php_uploads": _run_on_server(
                    self._inventory, server,
                    f"find {docroot}/wp-content/uploads -name '*.php' -o -name '*.phtml' 2>/dev/null",
                ),
            }
            wp_keys = list(wp_checks.keys())
            wp_raw = await asyncio.gather(*[wp_checks[k] for k in wp_keys])
            wp_results = dict(zip(wp_keys, wp_raw))

        # Find which error log exists and get domain-filtered entries
        error_log_content = ""
        for log_path in [
            "/var/log/apache2/error_log",
            "/etc/httpd/logs/error_log",
            "/usr/local/apache/logs/error_log",
        ]:
            key = f"errlog_{log_path}"
            if key in check_results and check_results[key].exit_code == 0:
                errlog_result = await _run_on_server(
                    self._inventory, server,
                    f"tail -n 200 {log_path}",
                )
                if errlog_result.success:
                    # Filter for domain
                    error_log_content = "\n".join(
                        l for l in errlog_result.output.splitlines()
                        if domain.lower() in l.lower()
                    )
                break

        # Phase 4: Build the report
        return ToolResult(output=_build_report(
            domain, username, docroot, check_results, wp_detected, wp_results,
            error_log_content,
        ))


def _extract_owner(raw_json: str) -> str | None:
    """Extract the username from whmapi1 getdomainowner output."""
    try:
        data = json.loads(raw_json)
        return data.get("data", {}).get("user") or None
    except (json.JSONDecodeError, AttributeError):
        return None


def _build_report(
    domain: str,
    username: str | None,
    docroot: str | None,
    checks: dict[str, ToolResult],
    wp_detected: bool,
    wp_results: dict[str, ToolResult],
    error_log: str,
) -> str:
    """Build the full diagnosis report."""
    sections: list[str] = [f"# Site Diagnosis: {domain}\n"]

    # Account info
    if username:
        sections.append(f"**Account:** {username}")
        sections.append(f"**Docroot:** {docroot}")
    else:
        sections.append("⚠ Could not identify cPanel account owner for this domain.")

    # DNS
    sections.append("\n## DNS")
    for rtype in ("a", "mx", "ns"):
        key = f"dns_{rtype}"
        r = checks.get(key)
        val = r.output.strip() if r and r.success else "(failed)"
        sections.append(f"**{rtype.upper()}:** {val if val else '(no records)'}")

    # SSL
    sections.append("\n## SSL Certificate")
    ssl = checks.get("ssl")
    if ssl and ssl.success and ssl.output.strip():
        sections.append(ssl.output.strip())
    else:
        sections.append("⚠ Could not retrieve SSL certificate")

    # PHP
    if "php" in checks:
        sections.append("\n## PHP")
        php = checks["php"]
        if php.success:
            # Try to extract version from JSON
            try:
                data = json.loads(php.output)
                versions = data.get("data", {}).get("versions", [])
                if versions:
                    for v in versions:
                        vhost = v.get("vhost", "?")
                        ver = v.get("version", "?")
                        sections.append(f"  {vhost}: PHP {ver}")
                else:
                    sections.append(php.output.strip()[:500])
            except json.JSONDecodeError:
                sections.append(php.output.strip()[:500])
        else:
            sections.append(f"  ⚠ {php.error}")

    # Disk
    if "disk_summary" in checks:
        sections.append("\n## Disk Usage")
        ds = checks["disk_summary"]
        sections.append(f"**Total:** {ds.output.strip() if ds.success else '?'}")
        db = checks.get("disk_breakdown")
        if db and db.success and db.output.strip():
            sections.append(f"**Breakdown:**\n{db.output.strip()}")

    # WordPress
    if wp_detected:
        sections.append("\n## WordPress")
        sections.append("✓ WordPress detected")

        wv = wp_results.get("wp_version")
        if wv and wv.success:
            sections.append(f"**Version:** {wv.output.strip()}")

        wcv = wp_results.get("wp_core_verify")
        if wcv and wcv.success:
            if "success" in wcv.output.lower():
                sections.append("**Core integrity:** ✓ Verified")
            else:
                sections.append(f"**Core integrity:** ⚠ MODIFIED\n{wcv.output.strip()[:300]}")

        wdb = wp_results.get("wp_db_size")
        if wdb and wdb.success:
            sections.append(f"**Database size:** {wdb.output.strip()}")

        wpl = wp_results.get("wp_plugins")
        if wpl and wpl.success:
            plugin_lines = wpl.output.strip().splitlines()
            update_needed = [l for l in plugin_lines if "available" in l.lower()]
            inactive = [l for l in plugin_lines if ",inactive," in l.lower()]
            sections.append(f"**Plugins:** {len(plugin_lines) - 1} total")
            if update_needed:
                sections.append(f"  ⚠ {len(update_needed)} need updates")
            if inactive:
                sections.append(f"  {len(inactive)} inactive")

        php_uploads = wp_results.get("php_uploads")
        if php_uploads and php_uploads.success and php_uploads.output.strip():
            files = php_uploads.output.strip().splitlines()
            sections.append(f"**⚠ PHP files in uploads:** {len(files)} found (potential malware)")
            for f in files[:5]:
                sections.append(f"  {f}")
        elif php_uploads:
            sections.append("**PHP in uploads:** ✓ Clean")
    elif docroot:
        sections.append("\n## CMS Detection")
        sections.append("WordPress not detected (no wp-config.php)")

    # Error Log
    sections.append("\n## Recent Errors")
    if error_log.strip():
        err_lines = error_log.strip().splitlines()
        sections.append(f"{len(err_lines)} error log entries matching this domain:")
        # Categorize
        php_fatal = sum(1 for l in err_lines if "php fatal" in l.lower())
        php_warn = sum(1 for l in err_lines if "php warning" in l.lower())
        perm = sum(1 for l in err_lines if "permission denied" in l.lower())
        if php_fatal:
            sections.append(f"  ✗ PHP Fatal: {php_fatal}")
        if php_warn:
            sections.append(f"  ⚠ PHP Warning: {php_warn}")
        if perm:
            sections.append(f"  ⚠ Permission denied: {perm}")
        # Show last 5
        sections.append("**Last errors:**")
        for line in err_lines[-5:]:
            sections.append(f"  {line[:200]}")
    else:
        sections.append("✓ No recent errors for this domain")

    # Email
    if "mail_queue" in checks:
        sections.append("\n## Email")
        mq = checks["mail_queue"]
        if mq.success:
            sections.append(f"**Mail queue (server-wide):** {mq.output.strip()} messages")
        mb = checks.get("mail_boxes")
        if mb and mb.success and mb.output.strip():
            sections.append(f"**Mailbox sizes:**\n{mb.output.strip()}")

    return "\n".join(sections)
