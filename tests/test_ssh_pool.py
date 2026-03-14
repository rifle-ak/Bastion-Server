"""Tests for SSH connection pool."""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.tools.ssh_pool import SSHPool


class TestSSHPoolUnit:
    """Test SSHPool logic without real SSH connections."""

    def test_init(self):
        pool = SSHPool()
        assert pool.active_connections == []

    @pytest.mark.asyncio
    async def test_close_all_empty(self):
        pool = SSHPool()
        await pool.close_all()
        assert pool.active_connections == []

    @pytest.mark.asyncio
    async def test_local_server_rejected(self):
        """Pool should reject local (non-SSH) servers."""
        pool = SSHPool()
        server_info = MagicMock()
        server_info.name = "localhost"
        server_info.definition.ssh = False

        result = await pool.run(server_info, "uptime")
        assert result.exit_code == 1
        assert "local" in result.error.lower()

    @pytest.mark.asyncio
    async def test_run_many_local_rejected(self):
        pool = SSHPool()
        server_info = MagicMock()
        server_info.name = "localhost"
        server_info.definition.ssh = False

        results = await pool.run_many(server_info, {"a": "uptime", "b": "df"})
        assert all(r.exit_code == 1 for r in results.values())

    @pytest.mark.asyncio
    async def test_no_key_path_error(self):
        """Pool should error if no SSH key is configured."""
        pool = SSHPool()
        server_info = MagicMock()
        server_info.name = "test-server"
        server_info.definition.ssh = True
        server_info.definition.key_path = None

        # Mock asyncssh import in _get_connection so it doesn't crash
        # The pool should still raise RuntimeError for missing key_path
        mock_asyncssh = MagicMock()
        with patch.dict("sys.modules", {"asyncssh": mock_asyncssh}):
            result = await pool.run(server_info, "uptime")
        assert result.exit_code == 1
        assert "key" in result.error.lower() or "No SSH key" in result.error

    @pytest.mark.asyncio
    async def test_get_lock_creates_per_server(self):
        pool = SSHPool()
        lock1 = await pool._get_lock("server1")
        lock2 = await pool._get_lock("server2")
        lock1_again = await pool._get_lock("server1")
        assert lock1 is lock1_again
        assert lock1 is not lock2
