"""Tests for the Pterodactyl Panel API tool helpers."""

from __future__ import annotations

import json
from unittest.mock import patch

from agent.tools.pterodactyl import _get_panel_config


class TestPanelConfig:
    def test_from_env(self):
        with patch.dict("os.environ", {
            "PTERODACTYL_URL": "https://panel.example.com",
            "PTERODACTYL_API_KEY": "test-api-key",
        }):
            result = _get_panel_config(None, "gameserver-01")
            assert result is not None
            url, key = result
            assert url == "https://panel.example.com"
            assert key == "test-api-key"

    def test_trailing_slash_stripped(self):
        with patch.dict("os.environ", {
            "PTERODACTYL_URL": "https://panel.example.com/",
            "PTERODACTYL_API_KEY": "key",
        }):
            result = _get_panel_config(None, "server")
            assert result is not None
            assert result[0] == "https://panel.example.com"

    def test_not_configured(self):
        with patch.dict("os.environ", {}, clear=True):
            result = _get_panel_config(None, "server")
            assert result is None

    def test_partial_config(self):
        with patch.dict("os.environ", {"PTERODACTYL_URL": "https://panel.example.com"}, clear=True):
            result = _get_panel_config(None, "server")
            assert result is None
