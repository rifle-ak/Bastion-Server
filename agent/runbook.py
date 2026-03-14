"""Symptom fingerprinting and runbook engine.

Automatically learns from past investigations. When the agent diagnoses
and fixes a problem, it saves the symptom pattern (fingerprint) and the
resolution. Next time the same symptoms appear, it can suggest or
auto-apply the fix — no API call needed for known issues.

Storage: SQLite database in the state directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

_STATE_DIR = Path(os.environ.get("BASTION_STATE_DIR", "./state"))
_DB_PATH = _STATE_DIR / "runbook.db"


@dataclass
class SymptomFingerprint:
    """A set of symptoms that identify a known issue."""
    symptoms: list[str]
    server: str = ""
    category: str = ""  # cpu, memory, disk, network, container, etc.

    @property
    def fingerprint(self) -> str:
        """Generate a stable hash from the symptom set."""
        # Sort symptoms for consistent hashing regardless of order
        normalized = sorted(s.lower().strip() for s in self.symptoms)
        raw = "|".join(normalized)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class RunbookEntry:
    """A known issue with its resolution."""
    fingerprint: str
    symptoms: list[str]
    root_cause: str
    resolution: str
    server: str = ""
    category: str = ""
    times_seen: int = 1
    first_seen: float = 0.0
    last_seen: float = 0.0
    last_resolution_success: bool = True
    notes: str = ""


class RunbookEngine:
    """Stores and retrieves known issue patterns and their fixes."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or _DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS runbook (
                fingerprint TEXT PRIMARY KEY,
                symptoms TEXT NOT NULL,
                root_cause TEXT NOT NULL,
                resolution TEXT NOT NULL,
                server TEXT DEFAULT '',
                category TEXT DEFAULT '',
                times_seen INTEGER DEFAULT 1,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                last_resolution_success INTEGER DEFAULT 1,
                notes TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS incident_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                server TEXT NOT NULL,
                fingerprint TEXT,
                symptoms TEXT NOT NULL,
                diagnosis TEXT DEFAULT '',
                resolution TEXT DEFAULT '',
                resolved INTEGER DEFAULT 0,
                operator_notes TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_incident_server
                ON incident_log(server);
            CREATE INDEX IF NOT EXISTS idx_incident_time
                ON incident_log(timestamp);
        """)
        self._conn.commit()

    def learn(
        self,
        symptoms: list[str],
        root_cause: str,
        resolution: str,
        server: str = "",
        category: str = "",
        success: bool = True,
        notes: str = "",
    ) -> RunbookEntry:
        """Record a diagnosis for future reference.

        If the same symptom fingerprint already exists, update it.
        """
        fp = SymptomFingerprint(symptoms=symptoms, server=server, category=category)
        now = time.time()

        existing = self._conn.execute(
            "SELECT * FROM runbook WHERE fingerprint = ?", (fp.fingerprint,)
        ).fetchone()

        if existing:
            self._conn.execute("""
                UPDATE runbook SET
                    times_seen = times_seen + 1,
                    last_seen = ?,
                    root_cause = ?,
                    resolution = ?,
                    last_resolution_success = ?,
                    notes = ?
                WHERE fingerprint = ?
            """, (now, root_cause, resolution, int(success), notes, fp.fingerprint))
            times_seen = existing["times_seen"] + 1
            first_seen = existing["first_seen"]
        else:
            self._conn.execute("""
                INSERT INTO runbook (
                    fingerprint, symptoms, root_cause, resolution,
                    server, category, times_seen, first_seen, last_seen,
                    last_resolution_success, notes
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            """, (
                fp.fingerprint, json.dumps(symptoms), root_cause, resolution,
                server, category, now, now, int(success), notes,
            ))
            times_seen = 1
            first_seen = now

        self._conn.commit()
        logger.info("runbook_learned", fingerprint=fp.fingerprint, category=category)

        return RunbookEntry(
            fingerprint=fp.fingerprint,
            symptoms=symptoms,
            root_cause=root_cause,
            resolution=resolution,
            server=server,
            category=category,
            times_seen=times_seen,
            first_seen=first_seen,
            last_seen=now,
            last_resolution_success=success,
            notes=notes,
        )

    def lookup(self, symptoms: list[str]) -> RunbookEntry | None:
        """Find a known resolution for a set of symptoms."""
        fp = SymptomFingerprint(symptoms=symptoms)
        row = self._conn.execute(
            "SELECT * FROM runbook WHERE fingerprint = ?", (fp.fingerprint,)
        ).fetchone()

        if not row:
            return None

        return RunbookEntry(
            fingerprint=row["fingerprint"],
            symptoms=json.loads(row["symptoms"]),
            root_cause=row["root_cause"],
            resolution=row["resolution"],
            server=row["server"],
            category=row["category"],
            times_seen=row["times_seen"],
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            last_resolution_success=bool(row["last_resolution_success"]),
            notes=row["notes"],
        )

    def search(self, keyword: str, limit: int = 10) -> list[RunbookEntry]:
        """Search runbook entries by keyword in symptoms, root cause, or resolution."""
        rows = self._conn.execute("""
            SELECT * FROM runbook
            WHERE symptoms LIKE ? OR root_cause LIKE ? OR resolution LIKE ?
            ORDER BY last_seen DESC
            LIMIT ?
        """, (f"%{keyword}%", f"%{keyword}%", f"%{keyword}%", limit)).fetchall()

        return [
            RunbookEntry(
                fingerprint=r["fingerprint"],
                symptoms=json.loads(r["symptoms"]),
                root_cause=r["root_cause"],
                resolution=r["resolution"],
                server=r["server"],
                category=r["category"],
                times_seen=r["times_seen"],
                first_seen=r["first_seen"],
                last_seen=r["last_seen"],
                last_resolution_success=bool(r["last_resolution_success"]),
                notes=r["notes"],
            )
            for r in rows
        ]

    def recent(self, limit: int = 10) -> list[RunbookEntry]:
        """Get the most recently seen known issues."""
        rows = self._conn.execute("""
            SELECT * FROM runbook ORDER BY last_seen DESC LIMIT ?
        """, (limit,)).fetchall()

        return [
            RunbookEntry(
                fingerprint=r["fingerprint"],
                symptoms=json.loads(r["symptoms"]),
                root_cause=r["root_cause"],
                resolution=r["resolution"],
                server=r["server"],
                category=r["category"],
                times_seen=r["times_seen"],
                first_seen=r["first_seen"],
                last_seen=r["last_seen"],
                last_resolution_success=bool(r["last_resolution_success"]),
                notes=r["notes"],
            )
            for r in rows
        ]

    def log_incident(
        self,
        server: str,
        symptoms: list[str],
        diagnosis: str = "",
        resolution: str = "",
        resolved: bool = False,
        notes: str = "",
    ) -> int:
        """Log an incident for history tracking."""
        fp = SymptomFingerprint(symptoms=symptoms)
        cursor = self._conn.execute("""
            INSERT INTO incident_log (
                timestamp, server, fingerprint, symptoms,
                diagnosis, resolution, resolved, operator_notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            time.time(), server, fp.fingerprint, json.dumps(symptoms),
            diagnosis, resolution, int(resolved), notes,
        ))
        self._conn.commit()
        return cursor.lastrowid or 0

    def get_server_history(self, server: str, limit: int = 20) -> list[dict[str, Any]]:
        """Get incident history for a specific server."""
        rows = self._conn.execute("""
            SELECT * FROM incident_log
            WHERE server = ?
            ORDER BY timestamp DESC
            LIMIT ?
        """, (server, limit)).fetchall()

        return [dict(r) for r in rows]

    def get_repeat_offenders(self, min_occurrences: int = 3) -> list[RunbookEntry]:
        """Find issues that keep recurring."""
        rows = self._conn.execute("""
            SELECT * FROM runbook
            WHERE times_seen >= ?
            ORDER BY times_seen DESC
        """, (min_occurrences,)).fetchall()

        return [
            RunbookEntry(
                fingerprint=r["fingerprint"],
                symptoms=json.loads(r["symptoms"]),
                root_cause=r["root_cause"],
                resolution=r["resolution"],
                server=r["server"],
                category=r["category"],
                times_seen=r["times_seen"],
                first_seen=r["first_seen"],
                last_seen=r["last_seen"],
                last_resolution_success=bool(r["last_resolution_success"]),
                notes=r["notes"],
            )
            for r in rows
        ]

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
