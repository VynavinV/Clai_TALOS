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
