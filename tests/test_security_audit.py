"""Tests for security_audit helper functions."""

from __future__ import annotations

from agent.tools.base import ToolResult
from agent.tools.security_audit import _build_security_report


def _ok(output: str = "") -> ToolResult:
    return ToolResult(output=output)


def _err() -> ToolResult:
    return ToolResult(error="fail", exit_code=1)


class TestBuildSecurityReport:
    def _base_data(self, **overrides):
        data = {
            "sshd_config": _ok("PasswordAuthentication no\nPermitRootLogin no\nPort 2222\nMaxAuthTries 3\n"),
            "open_ports": _ok("LISTEN  0  128  0.0.0.0:22  *:*  users:((\"sshd\",pid=1))"),
            "iptables": _ok("Chain INPUT\nACCEPT\nDROP\n"),
            "nft": _err(),
            "failed_logins": _ok("user1  ssh  10.0.0.1\n"),
            "login_users": _ok("root:x:0:0:root:/root:/bin/bash\nuser:x:1000:1000::/home/user:/bin/bash\n"),
            "kernel": _ok("5.15.0-100-generic"),
            "auto_updates": _ok("active"),
            "updates": _ok(""),
            "yum_updates": _err(),
            "world_writable": _ok(""),
            "suid": _ok("/usr/bin/sudo\n/usr/bin/passwd\n"),
            "root_procs": _ok("root 1 0.0 init\n"),
        }
        data.update(overrides)
        return data

    def test_secure_server_gets_a(self):
        report = _build_security_report("test-srv", self._base_data())
        assert "Security Score" in report
        assert "Grade: A" in report
        assert "Password auth disabled" in report
        assert "Root login disabled" in report

    def test_password_auth_enabled_deducts(self):
        report = _build_security_report("test-srv", self._base_data(
            sshd_config=_ok("PasswordAuthentication yes\nPermitRootLogin no\n"),
        ))
        assert "PasswordAuthentication enabled" in report
        # Score should be lower
        assert "Grade: A" not in report or "85" in report

    def test_root_login_enabled(self):
        report = _build_security_report("test-srv", self._base_data(
            sshd_config=_ok("PasswordAuthentication no\nPermitRootLogin yes\n"),
        ))
        assert "Root SSH login permitted" in report

    def test_exposed_mysql_port(self):
        report = _build_security_report("test-srv", self._base_data(
            open_ports=_ok("LISTEN  0  128  0.0.0.0:3306  *:*  users:((\"mysqld\",pid=1))"),
        ))
        assert "MySQL" in report
        assert "should not be public" in report

    def test_no_firewall(self):
        report = _build_security_report("test-srv", self._base_data(
            iptables=_ok("Chain INPUT\nChain FORWARD\nChain OUTPUT\n"),
            nft=_err(),
        ))
        assert "No firewall rules" in report

    def test_many_failed_logins(self):
        lines = "\n".join([f"user{i}  ssh  10.0.0.{i}" for i in range(30)])
        report = _build_security_report("test-srv", self._base_data(
            failed_logins=_ok(lines),
        ))
        assert "failed login" in report.lower()
        assert "fail2ban" in report

    def test_world_writable_files(self):
        report = _build_security_report("test-srv", self._base_data(
            world_writable=_ok("/etc/shadow\n/etc/passwd\n"),
        ))
        assert "world-writable" in report

    def test_protocol_1_critical(self):
        report = _build_security_report("test-srv", self._base_data(
            sshd_config=_ok("PasswordAuthentication no\nPermitRootLogin no\nProtocol 1\n"),
        ))
        assert "Protocol 1" in report
        assert "insecure" in report

    def test_nftables_detected(self):
        report = _build_security_report("test-srv", self._base_data(
            iptables=_err(),
            nft=_ok("table inet filter {\n  chain input {\n  }\n}\n"),
        ))
        assert "nftables active" in report
