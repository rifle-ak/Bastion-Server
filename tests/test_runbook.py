"""Tests for the runbook engine."""

from __future__ import annotations

import tempfile
from pathlib import Path

from agent.runbook import RunbookEngine, SymptomFingerprint


class TestSymptomFingerprint:
    def test_consistent_hash(self):
        fp1 = SymptomFingerprint(symptoms=["high cpu", "throttling"])
        fp2 = SymptomFingerprint(symptoms=["high cpu", "throttling"])
        assert fp1.fingerprint == fp2.fingerprint

    def test_order_independent(self):
        fp1 = SymptomFingerprint(symptoms=["throttling", "high cpu"])
        fp2 = SymptomFingerprint(symptoms=["high cpu", "throttling"])
        assert fp1.fingerprint == fp2.fingerprint

    def test_case_independent(self):
        fp1 = SymptomFingerprint(symptoms=["High CPU"])
        fp2 = SymptomFingerprint(symptoms=["high cpu"])
        assert fp1.fingerprint == fp2.fingerprint

    def test_different_symptoms_different_hash(self):
        fp1 = SymptomFingerprint(symptoms=["high cpu"])
        fp2 = SymptomFingerprint(symptoms=["high memory"])
        assert fp1.fingerprint != fp2.fingerprint


class TestRunbookEngine:
    def _make_engine(self):
        tmp = tempfile.mkdtemp()
        return RunbookEngine(db_path=Path(tmp) / "test_runbook.db")

    def test_learn_and_lookup(self):
        engine = self._make_engine()
        engine.learn(
            symptoms=["CPU throttled", "lag reported"],
            root_cause="Container CPU limit too low",
            resolution="Increased CPU allocation from 2 to 4 cores",
            server="gameserver-01",
            category="cpu",
        )

        result = engine.lookup(["CPU throttled", "lag reported"])
        assert result is not None
        assert result.root_cause == "Container CPU limit too low"
        assert result.times_seen == 1
        engine.close()

    def test_lookup_miss(self):
        engine = self._make_engine()
        result = engine.lookup(["unknown symptom"])
        assert result is None
        engine.close()

    def test_learn_updates_count(self):
        engine = self._make_engine()
        engine.learn(
            symptoms=["disk full"],
            root_cause="Logs not rotated",
            resolution="Set up logrotate",
        )
        engine.learn(
            symptoms=["disk full"],
            root_cause="Logs not rotated (again)",
            resolution="Fixed logrotate config",
        )
        result = engine.lookup(["disk full"])
        assert result is not None
        assert result.times_seen == 2
        assert result.root_cause == "Logs not rotated (again)"
        engine.close()

    def test_search(self):
        engine = self._make_engine()
        engine.learn(
            symptoms=["high memory"],
            root_cause="Memory leak in plugin",
            resolution="Updated plugin",
            category="memory",
        )
        results = engine.search("memory")
        assert len(results) == 1
        assert "memory" in results[0].root_cause.lower()
        engine.close()

    def test_log_incident(self):
        engine = self._make_engine()
        incident_id = engine.log_incident(
            server="gameserver-01",
            symptoms=["lag", "high cpu"],
            diagnosis="CPU throttling",
        )
        assert incident_id > 0

        history = engine.get_server_history("gameserver-01")
        assert len(history) == 1
        engine.close()

    def test_repeat_offenders(self):
        engine = self._make_engine()
        for _ in range(5):
            engine.learn(
                symptoms=["container crash"],
                root_cause="OOM",
                resolution="Increase memory",
            )
        offenders = engine.get_repeat_offenders(min_occurrences=3)
        assert len(offenders) == 1
        assert offenders[0].times_seen == 5
        engine.close()

    def test_recent(self):
        engine = self._make_engine()
        engine.learn(symptoms=["issue1"], root_cause="cause1", resolution="fix1")
        engine.learn(symptoms=["issue2"], root_cause="cause2", resolution="fix2")
        recent = engine.recent(limit=5)
        assert len(recent) == 2
        engine.close()
