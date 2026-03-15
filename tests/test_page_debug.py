"""Tests for page_debug helper functions."""

from __future__ import annotations

from agent.tools.page_debug import (
    _analyze_page,
    _check_code_blocks,
    _check_encoding,
    _check_html_structure,
    _detect_debug_output,
    _detect_mixed_content,
    _detect_php_errors,
)


class TestDetectPhpErrors:
    def test_fatal_error(self):
        body = '<b>Fatal error</b>: Call to undefined function foo() in /var/www/test.php on line 42'
        issues = _detect_php_errors(body)
        assert any("FATAL" in i for i in issues)

    def test_warning(self):
        body = '<b>Warning</b>: Division by zero in /var/www/test.php on line 10'
        issues = _detect_php_errors(body)
        assert any("WARNING" in i for i in issues)

    def test_xdebug(self):
        body = '<table class="xdebug-error">'
        issues = _detect_php_errors(body)
        assert any("Xdebug" in i for i in issues)

    def test_wp_database_error(self):
        body = '<div>WordPress database error</div>'
        issues = _detect_php_errors(body)
        assert any("WordPress database" in i for i in issues)

    def test_clean_page(self):
        body = '<html><body><h1>Hello World</h1></body></html>'
        assert _detect_php_errors(body) == []


class TestCheckHtmlStructure:
    def test_unclosed_pre(self):
        body = '<!DOCTYPE html><html><body><pre>some code<pre>more code</body></html>'
        issues = _check_html_structure(body)
        assert any("UNCLOSED <pre>" in i for i in issues)

    def test_unclosed_div(self):
        body = '<!DOCTYPE html><html><body><div>one<div>two</div></body></html>'
        issues = _check_html_structure(body)
        assert any("UNCLOSED <div>" in i for i in issues)

    def test_missing_doctype(self):
        body = '<html><body>no doctype</body></html>'
        issues = _check_html_structure(body)
        assert any("DOCTYPE" in i for i in issues)

    def test_raw_php_tag(self):
        body = '<!DOCTYPE html><html><body><?php echo "hi"; ?></body></html>'
        issues = _check_html_structure(body)
        assert any("RAW PHP" in i for i in issues)

    def test_clean_html(self):
        body = '<!DOCTYPE html><html><body><div>clean</div></body></html>'
        issues = _check_html_structure(body)
        assert len(issues) == 0


class TestCheckCodeBlocks:
    def test_unclosed_pre_code(self):
        body = '<pre><code>some code here'
        issues = _check_code_blocks(body)
        assert any("UNCLOSED CODE BLOCK" in i for i in issues)

    def test_proper_code_blocks(self):
        body = '<pre><code>code</code></pre>'
        assert _check_code_blocks(body) == []

    def test_html_in_code(self):
        body = '<code><div>rendered</div><span>tags</span><p>inside</p><a>code</a></code>'
        issues = _check_code_blocks(body)
        assert any("not escaped" in i for i in issues)


class TestDetectMixedContent:
    def test_http_resources(self):
        body = '<img src="http://example.com/img.jpg"><script src="http://cdn.example.com/js/app.js"></script>'
        issues = _detect_mixed_content(body)
        assert any("MIXED CONTENT" in i for i in issues)

    def test_no_mixed_content(self):
        body = '<img src="https://example.com/img.jpg">'
        assert _detect_mixed_content(body) == []


class TestCheckEncoding:
    def test_charset_mismatch(self):
        body = '<meta charset="ISO-8859-1">'
        headers = 'Content-Type: text/html; charset=UTF-8'
        issues = _check_encoding(body, headers)
        assert any("MISMATCH" in i for i in issues)

    def test_matching_charset(self):
        body = '<meta charset="utf-8">'
        headers = 'Content-Type: text/html; charset=utf-8'
        assert _check_encoding(body, headers) == []

    def test_replacement_characters(self):
        body = 'Hello \ufffd\ufffd\ufffd\ufffd\ufffd\ufffd world'
        issues = _check_encoding(body, "")
        assert any("replacement" in i for i in issues)


class TestDetectDebugOutput:
    def test_var_dump(self):
        body = '<pre>var_dump($data);</pre>'
        issues = _detect_debug_output(body)
        assert any("var_dump" in i for i in issues)

    def test_array_dump(self):
        body = '<pre>Array\n(\n    [0] => value\n)</pre>'
        issues = _detect_debug_output(body)
        assert any("Array dump" in i for i in issues)

    def test_clean_page(self):
        body = '<html><body>No debug output</body></html>'
        assert _detect_debug_output(body) == []


class TestAnalyzePage:
    def test_clean_page(self):
        headers = 'HTTP/1.1 200 OK\nContent-Type: text/html; charset=utf-8\nContent-Encoding: gzip\nCache-Control: max-age=3600\nX-Frame-Options: DENY\nX-Content-Type-Options: nosniff\nStrict-Transport-Security: max-age=31536000\nContent-Security-Policy: default-src self\nX-XSS-Protection: 1; mode=block'
        body = '<!DOCTYPE html><html><head></head><body><div>Hello</div></body></html>'
        report = _analyze_page("https://example.com", headers, body, "0|https://example.com|200")
        assert "No rendering issues" in report

    def test_broken_page(self):
        headers = 'HTTP/1.1 200 OK\nContent-Type: text/html'
        body = '<html><body><b>Fatal error</b>: something in test.php on line 1<pre>unclosed'
        report = _analyze_page("https://example.com", headers, body, "0|https://example.com|200")
        assert "FATAL" in report
        assert "UNCLOSED" in report or "Missing <!DOCTYPE" in report

    def test_empty_body(self):
        report = _analyze_page("https://example.com", "HTTP/1.1 200 OK", "", "")
        assert "Empty response" in report
