"""Session persistence for daemon mode.

Saves and loads conversation histories so that sessions can be resumed
after disconnection or token-limit interruption.  Sessions are stored
as JSON files in a configurable directory.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()


@dataclass
class SessionMeta:
    """Metadata about a saved session."""

    session_id: str
    created_at: float
    updated_at: float
    turns: int
    preview: str = ""


class SessionStore:
    """Manages session save/load on the filesystem.

    Each session is stored as a JSON file named ``{session_id}.json``
    inside the configured directory.
    """

    def __init__(self, sessions_dir: str | Path) -> None:
        self._dir = Path(sessions_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def create_id() -> str:
        """Generate a short, unique session ID."""
        return uuid.uuid4().hex[:12]

    def save(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        *,
        created_at: float | None = None,
    ) -> Path:
        """Persist a conversation to disk.

        Args:
            session_id: Unique session identifier.
            messages: The full message history list.
            created_at: Original creation timestamp (preserved across saves).

        Returns:
            Path to the saved session file.
        """
        now = time.time()
        preview = self._extract_preview(messages)
        data = {
            "session_id": session_id,
            "created_at": created_at or now,
            "updated_at": now,
            "turns": sum(1 for m in messages if m.get("role") == "user" and isinstance(m.get("content"), str)),
            "preview": preview,
            "messages": messages,
        }
        path = self._dir / f"{session_id}.json"
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, default=str)
        tmp.rename(path)
        logger.debug("session_saved", session_id=session_id, turns=data["turns"])
        return path

    def load(self, session_id: str) -> tuple[list[dict[str, Any]], float]:
        """Load a conversation from disk.

        Args:
            session_id: The session to load.

        Returns:
            Tuple of (messages, created_at).

        Raises:
            FileNotFoundError: If the session does not exist.
        """
        path = self._dir / f"{session_id}.json"
        with open(path) as f:
            data = json.load(f)
        messages = data.get("messages", [])
        created_at = data.get("created_at", time.time())
        logger.info("session_loaded", session_id=session_id, turns=len(messages))
        return messages, created_at

    def list_sessions(self, limit: int = 20) -> list[SessionMeta]:
        """List saved sessions, most recent first.

        Args:
            limit: Maximum number of sessions to return.

        Returns:
            List of SessionMeta sorted by updated_at descending.
        """
        sessions: list[SessionMeta] = []
        for path in self._dir.glob("*.json"):
            try:
                with open(path) as f:
                    data = json.load(f)
                sessions.append(SessionMeta(
                    session_id=data["session_id"],
                    created_at=data.get("created_at", 0),
                    updated_at=data.get("updated_at", 0),
                    turns=data.get("turns", 0),
                    preview=data.get("preview", ""),
                ))
            except (json.JSONDecodeError, KeyError, OSError) as e:
                logger.warning("session_file_corrupt", path=str(path), error=str(e))
                continue

        sessions.sort(key=lambda s: s.updated_at, reverse=True)
        return sessions[:limit]

    def delete(self, session_id: str) -> bool:
        """Delete a saved session.

        Returns:
            True if the session was deleted, False if it didn't exist.
        """
        path = self._dir / f"{session_id}.json"
        if path.exists():
            path.unlink()
            return True
        return False

    @staticmethod
    def _extract_preview(messages: list[dict[str, Any]], max_len: int = 80) -> str:
        """Extract a short preview from the first user message."""
        for msg in messages:
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                text = msg["content"].strip()
                if len(text) > max_len:
                    return text[:max_len] + "..."
                return text
        return ""
