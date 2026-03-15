"""Web page debugging and rendering issue detection.

Fetches a URL and analyzes the response for common issues that cause
broken rendering: unclosed HTML tags, PHP errors leaking into output,
broken code blocks, mixed content, missing assets, encoding issues,
and security header gaps.

This is the tool for "my HTML is bleeding out of the code block" and
"my client can see PHP errors on the page."
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from agent.inventory import Inventory
from agent.tools.base import BaseTool, ToolResult
from agent.tools.docker_tools import _run_on_server


class PageDebug(BaseTool):
    """Debug web page rendering issues — broken HTML, PHP errors, code bleeding."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory

    @property
    def name(self) -> str:
        return "page_debug"

    @property
    def description(self) -> str:
        return (
            "Debug web page rendering issues. Detects: unclosed HTML tags "
            "(code 'bleeding out'), PHP errors visible to visitors, broken "
            "entities, mixed content, missing assets, encoding issues, "
            "and security header gaps. Give it a URL."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "properties": {
                "server": {
                    "type": "string",
                    "description": "Server to fetch from (for local network access).",
                },
                "url": {
                    "type": "string",
                    "description": "Full URL to debug (e.g. 'https://example.com/page').",
                },
            },
            "required": ["server", "url"],
        }

    async def execute(self, *, server: str, url: str, **kwargs: Any) -> ToolResult:
        """Fetch URL and analyze for rendering issues."""
        # Fetch headers and body
        header_check = _run_on_server(
            self._inventory, server,
            f"curl -sI -L --max-time 15 '{url}'",
        )
        body_check = _run_on_server(
            self._inventory, server,
            f"curl -sL --max-time 15 '{url}'",
        )
        redirect_check = _run_on_server(
            self._inventory, server,
            f"curl -sIL --max-time 15 -w '%{{num_redirects}}|%{{url_effective}}|%{{http_code}}' -o /dev/null '{url}'",
        )

        headers_result, body_result, redirect_result = await asyncio.gather(
            header_check, body_check, redirect_check,
        )

        headers = headers_result.output if headers_result.success else ""
        body = body_result.output if body_result.success else ""
        redirect_info = redirect_result.output if redirect_result.success else ""

        if not body and not headers:
            return ToolResult(
                error=f"Could not fetch {url} — connection failed or timed out.",
                exit_code=1,
            )

        return ToolResult(output=_analyze_page(url, headers, body, redirect_info))


def _analyze_page(url: str, headers: str, body: str, redirect_info: str) -> str:
    """Analyze page content for rendering issues."""
    sections: list[str] = [f"# Page Debug: {url}\n"]
    findings: list[str] = []

    # ── Redirect Chain ──
    if redirect_info:
        parts = redirect_info.split("|")
        if len(parts) >= 3:
            num_redirects = parts[0]
            final_url = parts[1]
            final_code = parts[2]
            if num_redirects != "0":
                sections.append(f"**Redirects:** {num_redirects} → {final_url} (HTTP {final_code})")
                try:
                    if int(num_redirects) > 3:
                        findings.append(f"⚠ {num_redirects} redirects — excessive redirect chain")
                except ValueError:
                    pass

    # ── Response Headers ──
    sections.append("\n## Response Headers")
    if headers:
        # Content-Type
        ct_match = re.search(r'content-type:\s*(.+)', headers, re.IGNORECASE)
        if ct_match:
            content_type = ct_match.group(1).strip()
            sections.append(f"Content-Type: {content_type}")
            if "charset" not in content_type.lower() and "text/html" in content_type.lower():
                findings.append(
                    "⚠ No charset in Content-Type header. This can cause "
                    "character encoding issues (garbled text, broken symbols)."
                )

        # Security headers
        security_headers = {
            "x-frame-options": "X-Frame-Options (clickjacking protection)",
            "x-content-type-options": "X-Content-Type-Options (MIME sniffing)",
            "strict-transport-security": "HSTS (force HTTPS)",
            "content-security-policy": "Content-Security-Policy (XSS protection)",
            "x-xss-protection": "X-XSS-Protection",
        }
        missing_security: list[str] = []
        for header, desc in security_headers.items():
            if header not in headers.lower():
                missing_security.append(desc)
        if missing_security:
            sections.append(f"⚠ Missing security headers: {len(missing_security)}")
            for h in missing_security:
                sections.append(f"  - {h}")

        # Cache headers
        if "cache-control" not in headers.lower() and "expires" not in headers.lower():
            findings.append("⚠ No cache headers — browser re-downloads on every visit")

        # Compression
        if "content-encoding" in headers.lower():
            enc_match = re.search(r'content-encoding:\s*(\S+)', headers, re.IGNORECASE)
            if enc_match:
                sections.append(f"Compression: {enc_match.group(1)}")
        else:
            findings.append("⚠ No compression (gzip/br) — page sent uncompressed")

    if not body:
        sections.append("\n✗ Empty response body")
        return "\n".join(sections)

    body_size = len(body)
    sections.append(f"\n**Body size:** {body_size:,} bytes")

    # ── PHP Errors in Output ──
    sections.append("\n## PHP Error Detection")
    php_errors = _detect_php_errors(body)
    if php_errors:
        findings.extend(php_errors)
        for err in php_errors:
            sections.append(f"  {err}")
    else:
        sections.append("✓ No PHP errors visible in output")

    # ── Broken HTML Tags ──
    sections.append("\n## HTML Structure")
    html_issues = _check_html_structure(body)
    if html_issues:
        findings.extend(html_issues)
        for issue in html_issues:
            sections.append(f"  {issue}")
    else:
        sections.append("✓ No obvious HTML structure issues")

    # ── Code Block Bleeding ──
    code_issues = _check_code_blocks(body)
    if code_issues:
        findings.extend(code_issues)
        for issue in code_issues:
            sections.append(f"  {issue}")

    # ── Mixed Content ──
    sections.append("\n## Mixed Content")
    if url.startswith("https"):
        mixed = _detect_mixed_content(body)
        if mixed:
            findings.extend(mixed)
            for m in mixed:
                sections.append(f"  {m}")
        else:
            sections.append("✓ No mixed content detected")

    # ── Encoding Issues ──
    encoding_issues = _check_encoding(body, headers)
    if encoding_issues:
        findings.extend(encoding_issues)

    # ── BOM Detection ──
    if body[:3] == '\xef\xbb\xbf' or body[:3] == '\ufeff':
        findings.append(
            "✗ BOM (Byte Order Mark) detected at start of output. "
            "This invisible character can break page layout and headers. "
            "Remove BOM from PHP files (save as UTF-8 without BOM)."
        )

    # ── Inline Errors / Debug Output ──
    debug_issues = _detect_debug_output(body)
    if debug_issues:
        findings.extend(debug_issues)

    # ── Verdict ──
    sections.append("\n---")
    if findings:
        sections.append(f"\n## Findings ({len(findings)} issues)\n")
        critical = [f for f in findings if f.startswith("✗")]
        warnings = [f for f in findings if f.startswith("⚠")]
        for f in critical:
            sections.append(f)
        for f in warnings:
            sections.append(f)
    else:
        sections.append("\n✓ No rendering issues detected. Page looks clean.")

    return "\n".join(sections)


def _detect_php_errors(body: str) -> list[str]:
    """Detect PHP errors/warnings visible in page output."""
    issues: list[str] = []
    patterns = [
        (r'<b>Fatal error</b>:', "✗ PHP FATAL ERROR visible to visitors"),
        (r'<b>Warning</b>:', "✗ PHP WARNING visible to visitors"),
        (r'<b>Notice</b>:', "⚠ PHP NOTICE visible to visitors"),
        (r'<b>Parse error</b>:', "✗ PHP PARSE ERROR visible to visitors"),
        (r'<b>Deprecated</b>:', "⚠ PHP DEPRECATED notice visible"),
        (r'Stack trace:', "✗ Stack trace visible to visitors (security risk)"),
        (r'on line \d+', None),  # Generic PHP error line
        (r'Call Stack', "✗ Debug call stack visible to visitors"),
        (r'xdebug', "✗ Xdebug output visible to visitors (disable in production)"),
    ]

    for pattern, message in patterns:
        matches = re.findall(pattern, body, re.IGNORECASE)
        if matches and message:
            # Find the actual error text
            context = re.search(f'({pattern}.{{0,200}})', body, re.IGNORECASE)
            detail = ""
            if context:
                detail = f"\n    → {context.group(1)[:150]}"
            issues.append(f"{message}{detail}")

    # WordPress-specific: wp_die output
    if "wp-die" in body.lower() or "WordPress database error" in body:
        issues.append("✗ WordPress database error displayed to visitors")

    return issues[:10]  # Cap at 10


def _check_html_structure(body: str) -> list[str]:
    """Check for broken HTML that causes rendering issues."""
    issues: list[str] = []

    # Check for unclosed important tags
    important_tags = ["pre", "code", "table", "div", "form", "script", "style"]
    for tag in important_tags:
        opens = len(re.findall(f'<{tag}[\\s>]', body, re.IGNORECASE))
        closes = len(re.findall(f'</{tag}>', body, re.IGNORECASE))
        if opens > closes:
            diff = opens - closes
            issues.append(
                f"✗ UNCLOSED <{tag}>: {opens} opening vs {closes} closing tags. "
                f"{diff} unclosed tag(s) will cause content to 'bleed' — "
                f"everything after the unclosed tag renders wrong."
            )
        elif closes > opens:
            diff = closes - opens
            issues.append(f"⚠ Extra </{tag}>: {diff} closing tag(s) without matching opener")

    # Check for DOCTYPE
    if "<!doctype" not in body[:500].lower():
        issues.append(
            "⚠ Missing <!DOCTYPE html> — browser renders in quirks mode, "
            "which causes inconsistent layout across browsers."
        )

    # Stray PHP tags
    if "<?php" in body or "<?=" in body:
        issues.append(
            "✗ RAW PHP TAG visible in output. PHP is not being processed — "
            "likely a misconfigured handler or .html file with PHP code."
        )

    # Check for unescaped entities
    unescaped = re.findall(r'(?<![&<])<(?!/?[a-zA-Z!])[^>]*(?!>)', body[:5000])
    if len(unescaped) > 5:
        issues.append(
            f"⚠ {len(unescaped)} potentially unescaped '<' characters — "
            f"these can break HTML structure. Use &lt; in content."
        )

    return issues


def _check_code_blocks(body: str) -> list[str]:
    """Check for code block rendering issues."""
    issues: list[str] = []

    # Unclosed <pre><code> combinations
    pre_code_opens = len(re.findall(r'<pre[^>]*>\s*<code', body, re.IGNORECASE))
    code_pre_closes = len(re.findall(r'</code>\s*</pre>', body, re.IGNORECASE))
    if pre_code_opens > code_pre_closes:
        issues.append(
            "✗ UNCLOSED CODE BLOCK: <pre><code> opened but not properly closed. "
            "This causes code to 'bleed' into the rest of the page — "
            "everything after it appears monospace and unstyled."
        )

    # HTML inside code blocks that's not escaped
    code_blocks = re.findall(r'<code[^>]*>(.*?)</code>', body, re.DOTALL | re.IGNORECASE)
    for block in code_blocks[:10]:
        # Check if HTML tags inside the code block are being rendered
        inner_tags = re.findall(r'<(?!/?code)[a-zA-Z][^>]*>', block)
        if len(inner_tags) > 3:
            issues.append(
                "⚠ HTML tags inside <code> block are not escaped — "
                "they render as HTML instead of showing as code. "
                "Use htmlspecialchars() or &lt; entities."
            )
            break

    return issues


def _detect_mixed_content(body: str) -> list[str]:
    """Detect HTTP resources loaded on an HTTPS page."""
    issues: list[str] = []

    # Find http:// URLs in src/href attributes
    http_resources = re.findall(
        r'(?:src|href|action)=["\']http://([^"\']+)["\']',
        body, re.IGNORECASE,
    )
    if http_resources:
        unique = list(set(http_resources))[:5]
        issues.append(
            f"✗ MIXED CONTENT: {len(http_resources)} HTTP resources on HTTPS page. "
            f"Browsers block these, causing broken images/scripts."
        )
        for u in unique:
            issues.append(f"  → http://{u[:80]}")

    return issues


def _check_encoding(body: str, headers: str) -> list[str]:
    """Check for encoding issues."""
    issues: list[str] = []

    # Check if meta charset matches header
    meta_charset = re.search(r'<meta[^>]*charset=[\'"]*([^\'">;\s]+)', body[:2000], re.IGNORECASE)
    header_charset = re.search(r'charset=([^\s;]+)', headers, re.IGNORECASE)

    if meta_charset and header_charset:
        meta_cs = meta_charset.group(1).lower().replace("-", "")
        header_cs = header_charset.group(1).lower().replace("-", "")
        if meta_cs != header_cs:
            issues.append(
                f"✗ CHARSET MISMATCH: Header says {header_charset.group(1)}, "
                f"meta tag says {meta_charset.group(1)}. This causes garbled "
                f"text (mojibake). Make them match."
            )

    # Detect replacement characters (sign of encoding issues)
    replacement_count = body.count('\ufffd')
    if replacement_count > 5:
        issues.append(
            f"⚠ {replacement_count} replacement characters (�) detected — "
            f"content has encoding errors (probably Latin-1 data in UTF-8 page)"
        )

    return issues


def _detect_debug_output(body: str) -> list[str]:
    """Detect debug/development output in production."""
    issues: list[str] = []

    debug_patterns = [
        (r'var_dump\(', "var_dump() output"),
        (r'print_r\(', "print_r() output"),
        (r'<pre>Array\s*\(', "Array dump"),
        (r'Query Monitor', "Query Monitor plugin active"),
        (r'<!-- Debug:', "Debug HTML comments"),
        (r'console\.log\([\'"]debug', "console.log debug statements"),
    ]

    for pattern, label in debug_patterns:
        if re.search(pattern, body, re.IGNORECASE):
            issues.append(f"⚠ DEBUG OUTPUT: {label} visible in production page")

    return issues
