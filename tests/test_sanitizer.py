"""Tests for the input sanitizer."""

from __future__ import annotations

import pytest

from agent.security.sanitizer import SanitizationError, check_command, check_path, sanitize


class TestCheckCommand:
    """Tests for command sanitization."""

    def test_clean_commands_pass(self):
        """Normal commands should pass without error."""
        clean = ["uptime", "df -h", "free -h", "docker ps", "systemctl status nginx"]
        for cmd in clean:
            check_command(cmd)  # Should not raise

    def test_semicolon_rejected(self):
        with pytest.raises(SanitizationError, match="command chaining"):
            check_command("ls; rm -rf /")

    def test_pipe_rejected(self):
        with pytest.raises(SanitizationError, match="command chaining"):
            check_command("cat /etc/passwd | grep root")

    def test_ampersand_rejected(self):
        with pytest.raises(SanitizationError, match="command chaining"):
            check_command("sleep 100 &")

    def test_command_substitution_dollar_rejected(self):
        with pytest.raises(SanitizationError, match="command substitution"):
            check_command("echo $(whoami)")

    def test_backtick_rejected(self):
        with pytest.raises(SanitizationError, match="backtick"):
            check_command("echo `id`")

    def test_path_traversal_rejected(self):
        with pytest.raises(SanitizationError, match="path traversal"):
            check_command("cat ../../etc/shadow")

    def test_redirect_absolute_rejected(self):
        with pytest.raises(SanitizationError, match="redirect"):
            check_command("echo hacked > /etc/passwd")

    def test_append_absolute_rejected(self):
        with pytest.raises(SanitizationError, match="redirect|append"):
            check_command("echo hacked >> /etc/passwd")

    def test_eval_rejected(self):
        with pytest.raises(SanitizationError, match="eval"):
            check_command("eval dangerous_code")

    def test_exec_rejected(self):
        with pytest.raises(SanitizationError, match="exec"):
            check_command("exec /bin/sh")


class TestCheckPath:
    """Tests for path sanitization."""

    def test_clean_paths_pass(self):
        clean = ["/var/log/syslog", "/etc/hostname", "/home/user/file.txt"]
        for path in clean:
            check_path(path)  # Should not raise

    def test_traversal_rejected(self):
        with pytest.raises(SanitizationError, match="path traversal"):
            check_path("/var/log/../../etc/shadow")

    def test_shell_chars_rejected(self):
        with pytest.raises(SanitizationError, match="shell metacharacters"):
            check_path("/var/log/file;rm -rf /")

    def test_command_sub_in_path_rejected(self):
        with pytest.raises(SanitizationError, match="command substitution"):
            check_path("/var/log/$(whoami)")


class TestSanitize:
    """Tests for the full sanitize() function."""

    def test_clean_input_returned_unchanged(self):
        inp = {"command": "uptime", "server": "localhost"}
        result = sanitize("run_local", inp)
        assert result is inp

    def test_command_field_checked(self):
        with pytest.raises(SanitizationError):
            sanitize("run_local", {"command": "ls; rm /"})

    def test_path_field_checked(self):
        with pytest.raises(SanitizationError):
            sanitize("read_file", {"path": "/etc/../shadow"})

    def test_server_field_checked(self):
        with pytest.raises(SanitizationError, match="shell metacharacters"):
            sanitize("run_remote", {"server": "host;evil"})

    def test_container_field_checked(self):
        with pytest.raises(SanitizationError, match="shell metacharacters"):
            sanitize("docker_logs", {"container": "name$(id)"})

    def test_service_field_checked(self):
        with pytest.raises(SanitizationError, match="shell metacharacters"):
            sanitize("service_status", {"service": "svc`id`"})

    def test_no_special_fields_passes(self):
        result = sanitize("list_servers", {})
        assert result == {}
