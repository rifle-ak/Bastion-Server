"""Tests for config_diff report builders and pure helpers."""

from __future__ import annotations

from agent.tools.base import ToolResult
from agent.tools.config_diff import (
    BaselineCheck,
    DiffChange,
    _build_baseline_report,
    _build_diff_report,
    _check_docker_baseline,
    _check_mysql_baseline,
    _check_php_baseline,
    _check_ssh_baseline,
    _is_security_relevant,
    _paths_for_type,
    _simple_diff,
)


def _ok(output: str = "") -> ToolResult:
    return ToolResult(output=output)


def _err() -> ToolResult:
    return ToolResult(error="fail", exit_code=1)


# ---------------------------------------------------------------------------
# _simple_diff
# ---------------------------------------------------------------------------


class TestSimpleDiff:
    def test_identical_texts(self):
        text = "line1\nline2\nline3"
        assert _simple_diff(text, text) == []

    def test_modified_line(self):
        a = "Port 22\nProtocol 2"
        b = "Port 2222\nProtocol 2"
        changes = _simple_diff(a, b)
        assert len(changes) == 1
        assert changes[0].change_type == "modified"
        assert changes[0].line_number == 1
        assert changes[0].content_a == "Port 22"
        assert changes[0].content_b == "Port 2222"

    def test_added_lines(self):
        a = "line1"
        b = "line1\nline2\nline3"
        changes = _simple_diff(a, b)
        assert len(changes) == 2
        assert all(c.change_type == "added" for c in changes)

    def test_removed_lines(self):
        a = "line1\nline2\nline3"
        b = "line1"
        changes = _simple_diff(a, b)
        assert len(changes) == 2
        assert all(c.change_type == "removed" for c in changes)

    def test_empty_texts(self):
        assert _simple_diff("", "") == []

    def test_empty_vs_content(self):
        changes = _simple_diff("", "new line")
        assert len(changes) == 1
        assert changes[0].change_type == "added"
        assert changes[0].content_b == "new line"


# ---------------------------------------------------------------------------
# _is_security_relevant
# ---------------------------------------------------------------------------


class TestIsSecurityRelevant:
    def test_permit_root_login(self):
        assert _is_security_relevant("PermitRootLogin yes") is True

    def test_password_authentication(self):
        assert _is_security_relevant("PasswordAuthentication no") is True

    def test_bind_address(self):
        assert _is_security_relevant("bind-address = 127.0.0.1") is True

    def test_expose_php(self):
        assert _is_security_relevant("expose_php = On") is True

    def test_display_errors(self):
        assert _is_security_relevant("display_errors = Off") is True

    def test_not_security_relevant(self):
        assert _is_security_relevant("LogLevel debug") is False

    def test_empty_line(self):
        assert _is_security_relevant("") is False

    def test_live_restore(self):
        assert _is_security_relevant('"live-restore": true') is True


# ---------------------------------------------------------------------------
# _build_diff_report
# ---------------------------------------------------------------------------


class TestBuildDiffReport:
    def test_no_differences(self):
        config = {"/etc/ssh/sshd_config": "Port 22\nPermitRootLogin no"}
        report = _build_diff_report(config, config, "srv1", "srv2")
        assert "Total differences: 0" in report
        assert "Identical on both servers" in report

    def test_differences_detected(self):
        a = {"/etc/ssh/sshd_config": "Port 22"}
        b = {"/etc/ssh/sshd_config": "Port 2222"}
        report = _build_diff_report(a, b, "srv1", "srv2")
        assert "Total differences: 1" in report
        assert "1 difference(s) found" in report
        assert "srv1" in report
        assert "srv2" in report

    def test_security_relevant_flagged(self):
        a = {"/etc/ssh/sshd_config": "PermitRootLogin no"}
        b = {"/etc/ssh/sshd_config": "PermitRootLogin yes"}
        report = _build_diff_report(a, b, "srv1", "srv2")
        assert "[SECURITY]" in report

    def test_file_only_on_one_server(self):
        a = {"/etc/my.cnf": "bind-address = 127.0.0.1"}
        b: dict[str, str] = {}
        report = _build_diff_report(a, b, "srv1", "srv2")
        assert "Only present on srv1" in report
        assert "missing from srv2" in report

    def test_file_only_on_other_server(self):
        a: dict[str, str] = {}
        b = {"/etc/my.cnf": "bind-address = 0.0.0.0"}
        report = _build_diff_report(a, b, "srv1", "srv2")
        assert "Only present on srv2" in report
        assert "missing from srv1" in report

    def test_multiple_files(self):
        a = {
            "/etc/ssh/sshd_config": "Port 22",
            "/etc/my.cnf": "bind-address = 127.0.0.1",
        }
        b = {
            "/etc/ssh/sshd_config": "Port 22",
            "/etc/my.cnf": "bind-address = 0.0.0.0",
        }
        report = _build_diff_report(a, b, "srv1", "srv2")
        assert "Total differences: 1" in report
        assert "/etc/ssh/sshd_config" in report
        assert "Identical" in report
        assert "/etc/my.cnf" in report
        assert "[SECURITY]" in report

    def test_empty_configs(self):
        report = _build_diff_report({}, {}, "srv1", "srv2")
        assert "Total differences: 0" in report

    def test_added_line_format(self):
        a = {"f.conf": "line1"}
        b = {"f.conf": "line1\nline2"}
        report = _build_diff_report(a, b, "sA", "sB")
        assert "+sB:" in report

    def test_removed_line_format(self):
        a = {"f.conf": "line1\nline2"}
        b = {"f.conf": "line1"}
        report = _build_diff_report(a, b, "sA", "sB")
        assert "-sA:" in report


# ---------------------------------------------------------------------------
# _check_ssh_baseline
# ---------------------------------------------------------------------------


class TestCheckSSHBaseline:
    def test_secure_config(self):
        content = "PermitRootLogin no\nPasswordAuthentication no\nPort 2222"
        checks = _check_ssh_baseline(content)
        statuses = {c.setting: c.status for c in checks}
        assert statuses["PermitRootLogin"] == "PASS"
        assert statuses["PasswordAuthentication"] == "PASS"
        assert statuses["Port"] == "PASS"

    def test_root_login_yes(self):
        content = "PermitRootLogin yes\nPasswordAuthentication no"
        checks = _check_ssh_baseline(content)
        root = next(c for c in checks if c.setting == "PermitRootLogin")
        assert root.status == "FAIL"

    def test_root_login_prohibit_password(self):
        content = "PermitRootLogin prohibit-password"
        checks = _check_ssh_baseline(content)
        root = next(c for c in checks if c.setting == "PermitRootLogin")
        assert root.status == "WARN"

    def test_password_auth_not_set(self):
        content = "PermitRootLogin no"
        checks = _check_ssh_baseline(content)
        pw = next(c for c in checks if c.setting == "PasswordAuthentication")
        assert pw.status == "WARN"

    def test_default_port(self):
        content = "PermitRootLogin no\nPort 22"
        checks = _check_ssh_baseline(content)
        port = next(c for c in checks if c.setting == "Port")
        assert port.status == "WARN"

    def test_port_not_set(self):
        content = "PermitRootLogin no"
        checks = _check_ssh_baseline(content)
        port = next(c for c in checks if c.setting == "Port")
        assert port.status == "WARN"
        assert "defaults to 22" in port.explanation

    def test_comments_ignored(self):
        content = "# PermitRootLogin yes\nPermitRootLogin no"
        checks = _check_ssh_baseline(content)
        root = next(c for c in checks if c.setting == "PermitRootLogin")
        assert root.status == "PASS"


# ---------------------------------------------------------------------------
# _check_php_baseline
# ---------------------------------------------------------------------------


class TestCheckPHPBaseline:
    def test_secure_config(self):
        content = (
            "expose_php = Off\n"
            "display_errors = Off\n"
            "memory_limit = 256M\n"
            "disable_functions = exec,system,passthru,shell_exec\n"
        )
        checks = _check_php_baseline(content)
        statuses = {c.setting: c.status for c in checks}
        assert statuses["expose_php"] == "PASS"
        assert statuses["display_errors"] == "PASS"
        assert statuses["memory_limit"] == "PASS"
        assert statuses["disable_functions"] == "PASS"

    def test_expose_php_on(self):
        content = "expose_php = On\ndisplay_errors = Off"
        checks = _check_php_baseline(content)
        exp = next(c for c in checks if c.setting == "expose_php")
        assert exp.status == "WARN"

    def test_display_errors_on(self):
        content = "expose_php = Off\ndisplay_errors = On"
        checks = _check_php_baseline(content)
        de = next(c for c in checks if c.setting == "display_errors")
        assert de.status == "WARN"

    def test_disable_functions_empty(self):
        content = "expose_php = Off\ndisplay_errors = Off\ndisable_functions ="
        checks = _check_php_baseline(content)
        df = next(c for c in checks if c.setting == "disable_functions")
        assert df.status == "WARN"

    def test_missing_values_produce_warns(self):
        content = ""
        checks = _check_php_baseline(content)
        statuses = {c.setting: c.status for c in checks}
        assert statuses["expose_php"] == "WARN"
        assert statuses["display_errors"] == "WARN"
        assert statuses["memory_limit"] == "WARN"
        assert statuses["disable_functions"] == "WARN"

    def test_upload_max_filesize(self):
        content = "upload_max_filesize = 50M"
        checks = _check_php_baseline(content)
        up = next(c for c in checks if c.setting == "upload_max_filesize")
        assert up.status == "PASS"
        assert "50M" in up.explanation


# ---------------------------------------------------------------------------
# _check_mysql_baseline
# ---------------------------------------------------------------------------


class TestCheckMySQLBaseline:
    def test_secure_config(self):
        content = "bind-address = 127.0.0.1\nmax_connections = 200"
        checks = _check_mysql_baseline(content)
        statuses = {c.setting: c.status for c in checks}
        assert statuses["bind-address"] == "PASS"
        assert statuses["skip-grant-tables"] == "PASS"
        assert statuses["max_connections"] == "PASS"

    def test_bind_all_interfaces(self):
        content = "bind-address = 0.0.0.0"
        checks = _check_mysql_baseline(content)
        ba = next(c for c in checks if c.setting == "bind-address")
        assert ba.status == "FAIL"

    def test_bind_address_not_set(self):
        content = "max_connections = 100"
        checks = _check_mysql_baseline(content)
        ba = next(c for c in checks if c.setting == "bind-address")
        assert ba.status == "WARN"

    def test_skip_grant_tables(self):
        content = "skip-grant-tables\nbind-address = 127.0.0.1"
        checks = _check_mysql_baseline(content)
        sgt = next(c for c in checks if c.setting == "skip-grant-tables")
        assert sgt.status == "FAIL"
        assert "bypassed" in sgt.explanation.lower()

    def test_high_max_connections(self):
        content = "bind-address = 127.0.0.1\nmax_connections = 1000"
        checks = _check_mysql_baseline(content)
        mc = next(c for c in checks if c.setting == "max_connections")
        assert mc.status == "WARN"

    def test_bind_ipv6_all(self):
        content = "bind-address = ::"
        checks = _check_mysql_baseline(content)
        ba = next(c for c in checks if c.setting == "bind-address")
        assert ba.status == "FAIL"


# ---------------------------------------------------------------------------
# _check_docker_baseline
# ---------------------------------------------------------------------------


class TestCheckDockerBaseline:
    def test_secure_config(self):
        content = '{"live-restore": true, "log-driver": "json-file", "userns-remap": "default"}'
        checks = _check_docker_baseline(content)
        statuses = {c.setting: c.status for c in checks}
        assert statuses["live-restore"] == "PASS"
        assert statuses["log-rotation"] == "PASS"
        assert statuses["userns-remap"] == "PASS"

    def test_live_restore_false(self):
        content = '{"live-restore": false}'
        checks = _check_docker_baseline(content)
        lr = next(c for c in checks if c.setting == "live-restore")
        assert lr.status == "WARN"

    def test_missing_all(self):
        content = "{}"
        checks = _check_docker_baseline(content)
        statuses = {c.setting: c.status for c in checks}
        assert statuses["live-restore"] == "WARN"
        assert statuses["log-rotation"] == "WARN"
        assert statuses["userns-remap"] == "WARN"

    def test_max_size_counts_as_log_rotation(self):
        content = '{"log-opts": {"max-size": "10m"}}'
        checks = _check_docker_baseline(content)
        lr = next(c for c in checks if c.setting == "log-rotation")
        assert lr.status == "PASS"


# ---------------------------------------------------------------------------
# _build_baseline_report
# ---------------------------------------------------------------------------


class TestBuildBaselineReport:
    def test_all_pass(self):
        checks = [
            BaselineCheck("SSH", "PermitRootLogin", "PASS", "Root login disabled."),
            BaselineCheck("SSH", "PasswordAuthentication", "PASS", "Passwords disabled."),
        ]
        report = _build_baseline_report(checks, "srv1")
        assert "PASS: 2" in report
        assert "WARN: 0" in report
        assert "FAIL: 0" in report
        assert "## SSH" in report

    def test_mixed_results(self):
        checks = [
            BaselineCheck("SSH", "PermitRootLogin", "PASS", "OK"),
            BaselineCheck("SSH", "Port", "WARN", "Default port"),
            BaselineCheck("MySQL", "bind-address", "FAIL", "Open to all"),
        ]
        report = _build_baseline_report(checks, "srv1")
        assert "PASS: 1" in report
        assert "WARN: 1" in report
        assert "FAIL: 1" in report
        assert "## SSH" in report
        assert "## MySQL" in report
        assert "[PASS]" in report
        assert "[WARN]" in report
        assert "[FAIL]" in report

    def test_empty_checks(self):
        report = _build_baseline_report([], "srv1")
        assert "Baseline Report: srv1" in report
        assert "PASS: 0" in report

    def test_grouped_by_category(self):
        checks = [
            BaselineCheck("SSH", "PermitRootLogin", "PASS", "OK"),
            BaselineCheck("PHP", "expose_php", "WARN", "On"),
            BaselineCheck("SSH", "Port", "WARN", "22"),
        ]
        report = _build_baseline_report(checks, "srv1")
        # SSH section should appear and contain both SSH checks
        assert "## SSH" in report
        assert "## PHP" in report

    def test_server_name_in_header(self):
        report = _build_baseline_report([], "my-game-server")
        assert "my-game-server" in report


# ---------------------------------------------------------------------------
# _paths_for_type
# ---------------------------------------------------------------------------


class TestPathsForType:
    def test_cpanel_includes_general(self):
        paths = _paths_for_type("cpanel")
        assert "/etc/ssh/sshd_config" in paths
        assert "/etc/my.cnf" in paths

    def test_pterodactyl_includes_general(self):
        paths = _paths_for_type("pterodactyl")
        assert "/etc/ssh/sshd_config" in paths
        assert "/etc/docker/daemon.json" in paths

    def test_general(self):
        paths = _paths_for_type("general")
        assert "/etc/ssh/sshd_config" in paths
        assert "/etc/sysctl.conf" in paths

    def test_unknown_defaults_to_general(self):
        assert _paths_for_type("unknown") == _paths_for_type("general")
