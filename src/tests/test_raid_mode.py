"""Tests for raid mode: db toggle, lifted limits, swarm doctrine, quiet subagents."""

import asyncio
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TALOS_LAZY_TOOLS", "0")

import AI
import db


def _fresh_user(monkeypatch, tmp_path):
    """Isolated sqlite so user_settings changes do not touch real data."""
    test_db = str(tmp_path / "raid.db")
    real_connect = sqlite3.connect

    def _connect(path=test_db):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = real_connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    monkeypatch.setattr(db, "_conn", _connect)
    db.init()


def test_raid_mode_db_roundtrip(tmp_path, monkeypatch):
    _fresh_user(monkeypatch, tmp_path)
    assert db.get_raid_mode(1) is False
    assert db.set_raid_mode(1, True) is True
    assert db.get_raid_mode(1) is True
    assert db.set_raid_mode(1, False) is False
    assert db.get_raid_mode(1) is False


def test_agent_limits_lifted_in_raid():
    normal_rounds, normal_calls, _ = AI._agent_limits_for_speed("normal", raid=False)
    raid_rounds, raid_calls, _ = AI._agent_limits_for_speed("normal", raid=True)
    assert raid_rounds == normal_rounds * 2
    assert raid_calls >= 50

    sub_normal = AI._subagent_limits_for_speed("normal", raid=False)
    sub_raid = AI._subagent_limits_for_speed("normal", raid=True)
    assert sub_raid[0] == sub_normal[0] * 2
    assert sub_raid[1] == sub_normal[1] * 2


def test_background_subagent_cap_in_raid():
    assert AI.background_subagent_cap(False) == AI._BACKGROUND_SUBAGENT_MAX
    assert AI.background_subagent_cap(True) == AI.RAID_BACKGROUND_SUBAGENT_MAX
    assert AI.background_subagent_cap(True) >= 100
    assert AI.background_subagent_cap(True) > AI.background_subagent_cap(False)


def test_raid_prompt_included_only_in_raid(tmp_path, monkeypatch):
    _fresh_user(monkeypatch, tmp_path)
    off = AI._build_system(1, "hello", include_memories=False, raid_mode=False) or ""
    on = AI._build_system(1, "hello", include_memories=False, raid_mode=True) or ""
    assert "RAID MODE ACTIVE" not in off
    assert "RAID MODE ACTIVE" in on
    # The doctrine covers the orchestration requirements.
    assert "non-overlapping" in on
    assert "PLAN BEFORE YOU SPAWN" in on
    assert "YOU ARE THE CONDUCTOR" in on
    assert "do NOT message the user" in on
    # The orchestrator delegates deliverables; it does not produce them itself.
    assert "NOT THE PRODUCER" in on
    assert "You do NOT write deliverables yourself" in on
    assert "revision notes back to the producing agent" in on
    # Raid pushes work down harder, with double coverage on critical paths.
    assert "under-spawning" in on
    assert "DOUBLE-PUSH critical paths" in on


def test_orchestrator_doctrine_delegates_everything(tmp_path, monkeypatch):
    """The conductor doctrine: all work is delegated, even simple tasks."""
    _fresh_user(monkeypatch, tmp_path)
    system = AI._build_system(1, "hello", include_memories=False, raid_mode=False) or ""
    assert "You are the orchestrator — a CONDUCTOR, not a worker" in system
    assert "DELEGATE EVERYTHING" in system
    # Even simple one-step tasks get a subagent.
    assert "Even simple one-step tasks: spawn a subagent to do and deliver it" in system
    # Slideshows/deck building is explicitly delegated with the create_project pattern.
    assert "deck-builder that calls create_project" in system
    # Self-confirmation before working.
    assert "CONFIRM BEFORE YOU WORK" in system
    # The orchestrator frees itself with background subagents.
    assert "spawn with background=true and END your turn" in system
    # Direct answers are for pure conversation only.
    assert "pure conversation that needs no tools" in system


def test_raid_subagents_are_silent(tmp_path, monkeypatch):
    _fresh_user(monkeypatch, tmp_path)
    normal = AI._build_subagent_system(1, "researcher", "find X") or ""
    raid = AI._build_subagent_system(1, "researcher", "find X", raid_mode=True) or ""
    # Normal mode: Telegram updates allowed.
    assert "send_telegram_message" in normal
    # Raid mode: report through the final result, not the user.
    assert "Do NOT send Telegram messages to the user" in raid
    assert "NEEDS USER" in raid
    assert "complete, structured, and integration-ready" in raid


def test_toggle_round_trip_through_text():
    text_on = __import__("telegram_bot").raid_status_text(True)
    assert "RAID MODE ON" in text_on and "token heavy" in text_on.lower()
    text_off = __import__("telegram_bot").raid_status_text(False)
    assert "RAID MODE OFF" in text_off


def test_stop_background_subagents_cancels_swarm():
    """The emergency stop must actually cancel running background agents."""
    import AI

    cancelled = []

    class _FakeTask:
        def __init__(self):
            self.done = lambda: False

        def cancel(self):
            cancelled.append(1)

    entries = {
        "sub-1": {"id": "sub-1", "status": "running", "user_id": 1, "task": _FakeTask()},
        "sub-2": {"id": "sub-2", "status": "running", "user_id": 2, "task": _FakeTask()},
        "sub-3": {"id": "sub-3", "status": "done", "user_id": 1, "task": _FakeTask()},
        "sub-4": {"id": "sub-4", "status": "running", "user_id": 1, "task": None},  # legacy: no handle
    }
    original = dict(AI._BACKGROUND_SUBAGENTS)
    AI._BACKGROUND_SUBAGENTS.clear()
    AI._BACKGROUND_SUBAGENTS.update(entries)
    try:
        stopped = AI.stop_background_subagents(1)
        # Own running agents cancelled; other users untouched; done skipped.
        # Only agents with a live task handle count as "stopped" — sub-4 has
        # none (legacy format), so it is just marked cancelled.
        assert stopped == 1
        assert len(cancelled) == 1  # only sub-1 had a cancellable task
        assert entries["sub-1"]["status"] == "cancelled"
        assert entries["sub-4"]["status"] == "cancelled"
        assert entries["sub-2"]["status"] == "running"
        assert entries["sub-3"]["status"] == "done"
    finally:
        AI._BACKGROUND_SUBAGENTS.clear()
        AI._BACKGROUND_SUBAGENTS.update(original)


def test_stop_user_runs_cancels_registered_run():
    """core.stop_user_runs cancels the registered orchestrator task + swarm."""
    import core

    cancelled = []

    class _FakeTask:
        def done(self):
            return False

        def cancel(self):
            cancelled.append("run")

    def fake_stop_agents(user_id):
        cancelled.append("agents")
        return 3

    original_runs = dict(core._ACTIVE_RUNS)
    original_stop = AI.stop_background_subagents
    core._ACTIVE_RUNS.clear()
    core._ACTIVE_RUNS[7] = _FakeTask()
    AI.stop_background_subagents = fake_stop_agents
    try:
        result = core.stop_user_runs(7)
        assert result == {"run_stopped": True, "agents_stopped": 3}
        assert cancelled == ["agents", "run"]  # swarm first, then the run
        assert 7 not in core._ACTIVE_RUNS or core._ACTIVE_RUNS.get(7) is not None
    finally:
        core._ACTIVE_RUNS.clear()
        core._ACTIVE_RUNS.update(original_runs)
        AI.stop_background_subagents = original_stop

    # No run registered: nothing crashed, agents still stopped.
    core._ACTIVE_RUNS.clear()
    AI.stop_background_subagents = fake_stop_agents
    try:
        result = core.stop_user_runs(7)
        assert result["run_stopped"] is False
        assert result["agents_stopped"] == 3
    finally:
        AI.stop_background_subagents = original_stop
