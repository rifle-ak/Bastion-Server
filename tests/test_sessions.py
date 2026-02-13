"""Tests for the session persistence module."""

from __future__ import annotations

import json
import time

import pytest

from agent.sessions import SessionStore


@pytest.fixture
def store(tmp_path):
    """Create a SessionStore backed by a temp directory."""
    return SessionStore(tmp_path / "sessions")


@pytest.fixture
def sample_messages():
    """Return a minimal conversation history."""
    return [
        {"role": "user", "content": "check disk space"},
        {"role": "assistant", "content": [{"type": "text", "text": "Disk is at 42%."}]},
    ]


class TestSessionStore:
    def test_create_id_is_12_hex(self, store):
        sid = store.create_id()
        assert len(sid) == 12
        int(sid, 16)  # raises ValueError if not hex

    def test_save_and_load(self, store, sample_messages):
        sid = store.create_id()
        store.save(sid, sample_messages)
        messages, created_at = store.load(sid)
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "check disk space"
        assert created_at > 0

    def test_save_preserves_created_at(self, store, sample_messages):
        sid = store.create_id()
        original_time = 1700000000.0
        store.save(sid, sample_messages, created_at=original_time)
        _, created_at = store.load(sid)
        assert created_at == original_time

    def test_load_missing_raises(self, store):
        with pytest.raises(FileNotFoundError):
            store.load("nonexistent123")

    def test_list_sessions_empty(self, store):
        assert store.list_sessions() == []

    def test_list_sessions_returns_sorted(self, store, sample_messages):
        ids = []
        for i in range(3):
            sid = store.create_id()
            ids.append(sid)
            store.save(sid, sample_messages, created_at=1700000000.0 + i)
            time.sleep(0.01)  # Ensure distinct updated_at

        sessions = store.list_sessions()
        assert len(sessions) == 3
        # Most recent first
        assert sessions[0].session_id == ids[2]

    def test_list_sessions_limit(self, store, sample_messages):
        for _ in range(5):
            store.save(store.create_id(), sample_messages)
        assert len(store.list_sessions(limit=3)) == 3

    def test_delete(self, store, sample_messages):
        sid = store.create_id()
        store.save(sid, sample_messages)
        assert store.delete(sid) is True
        assert store.delete(sid) is False
        with pytest.raises(FileNotFoundError):
            store.load(sid)

    def test_preview_extracted(self, store, sample_messages):
        sid = store.create_id()
        store.save(sid, sample_messages)
        sessions = store.list_sessions()
        assert sessions[0].preview == "check disk space"

    def test_preview_long_truncated(self, store):
        long_msg = "x" * 200
        messages = [{"role": "user", "content": long_msg}]
        sid = store.create_id()
        store.save(sid, messages)
        sessions = store.list_sessions()
        assert sessions[0].preview.endswith("...")
        assert len(sessions[0].preview) <= 83  # 80 + "..."

    def test_turns_count(self, store):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            {"role": "user", "content": "how are you"},
            {"role": "assistant", "content": [{"type": "text", "text": "good"}]},
        ]
        sid = store.create_id()
        store.save(sid, messages)
        sessions = store.list_sessions()
        assert sessions[0].turns == 2

    def test_corrupt_file_skipped_in_list(self, store, sample_messages, tmp_path):
        """A corrupt JSON file should not crash list_sessions."""
        sid = store.create_id()
        store.save(sid, sample_messages)

        # Write a corrupt file
        bad_path = store._dir / "bad_session.json"
        bad_path.write_text("not valid json{{{")

        sessions = store.list_sessions()
        assert len(sessions) == 1  # only the valid one
