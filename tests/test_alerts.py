"""Tests for Discord webhook alerting."""

from __future__ import annotations

from agent.alerts import _truncate_for_discord


class TestTruncateForDiscord:
    def test_extracts_issues(self):
        text = (
            "## gameserver-01\n"
            "Uptime: 5 days\n"
            "Disk: OK\n"
            "  ⚠ Memory: 92% used\n"
            "  ✗ Container nginx: exited\n"
            "\n"
            "## webhost-01\n"
            "✓ All clear\n"
        )
        result = _truncate_for_discord(text)
        assert "gameserver-01" in result
        assert "Memory" in result
        assert "nginx" in result
        assert "webhost-01" in result
        # Normal lines should be excluded
        assert "Uptime: 5 days" not in result
        assert "Disk: OK" not in result

    def test_empty_input(self):
        result = _truncate_for_discord("")
        assert result == ""

    def test_truncation(self):
        # Create a very long string of issues
        lines = [f"⚠ Issue {i}" for i in range(500)]
        text = "\n".join(lines)
        result = _truncate_for_discord(text, max_len=200)
        assert len(result) <= 200
        assert "truncated" in result

    def test_no_issues(self):
        text = "## server1\n✓ All clear\n## server2\n✓ All clear"
        result = _truncate_for_discord(text)
        # Headers still included
        assert "server1" in result
        assert "server2" in result

    def test_issue_count_line(self):
        text = "## server1\n**3 issue(s):**\n  ⚠ High load\n  ⚠ Disk 90%\n  ✗ OOM\n"
        result = _truncate_for_discord(text)
        assert "issue" in result
        assert "High load" in result
        assert "OOM" in result
