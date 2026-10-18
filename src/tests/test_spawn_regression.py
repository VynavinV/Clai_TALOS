"""Regression tests for the raid-mode background spawn path.

The background spawn gate used to reference `raid_mode` outside its scope,
crashing every spawn_subagent(background=true) call with a NameError. These
tests execute the real spawn branch end to end so that class of bug cannot
silently return.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import AI
import laya_decisions  # noqa: F401 - ensures decision layer import is fine


def _mock_runner(monkeypatch, result_text="done"):
    calls = {"n": 0}

    async def fake_run_subagent(user_id, role, task, ctx, send_func=None, **kwargs):
        calls["n"] += 1
        return json.dumps({"ok": True, "result": result_text, "role": role})

    monkeypatch.setattr(AI, "_run_subagent", fake_run_subagent)

    async def fake_send(msg="", **kw):
        return None

    return calls, fake_send


def _clear_swarm():
    AI._BACKGROUND_SUBAGENTS.clear()


def test_background_spawn_does_not_crash(monkeypatch):
    """spawn_subagent(background=true) must execute the gate and spawn."""
    calls, send_func = _mock_runner(monkeypatch)

    import db

    monkeypatch.setattr(db, "get_raid_mode", lambda user_id: False)

    _clear_swarm()
    original = dict(AI._BACKGROUND_SUBAGENTS)

    async def _run_test():
        result = await AI._execute_tool_call(
            "spawn_subagent",
            {"task": "research the topic", "role": "researcher", "background": True},
            1,
            send_func=send_func,
            allow_subagent=True,
        )
        # The background runner is a detached task: give the loop a beat so
        # the mocked subagent actually executes before we assert.
        await asyncio.sleep(0.2)
        return result

    try:
        result = asyncio.run(_run_test())
    finally:
        AI._BACKGROUND_SUBAGENTS.clear()
        AI._BACKGROUND_SUBAGENTS.update(original)

    parsed = json.loads(result)
    assert parsed.get("ok") is True, parsed
    assert parsed.get("background") is True
    assert parsed.get("subagent_id", "").startswith("sub-researcher-")
    assert calls["n"] == 1


def test_background_spawn_cap_enforced(monkeypatch, tmp_path):
    """The swarm cap is enforced with raid's higher limit when raid is on."""
    import sqlite3

    import db

    _mock_runner(monkeypatch)
    _clear_swarm()

    test_db = str(tmp_path / "raid_spawn.db")
    real_connect = sqlite3.connect

    def _connect(path=test_db):
        conn = real_connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    monkeypatch.setattr(db, "_conn", _connect)
    db.init()
    db.set_raid_mode(1, True)

    # Fill the registry to raid's cap with running entries.
    cap = AI.background_subagent_cap(True)
    for i in range(cap):
        AI._BACKGROUND_SUBAGENTS[f"sub-fill-{i}"] = {"id": f"sub-fill-{i}", "status": "running", "user_id": 1}

    result = asyncio.run(
        AI._execute_tool_call(
            "spawn_subagent",
            {"task": "one too many", "background": True},
            1,
            send_func=None,
            allow_subagent=True,
        )
    )
    parsed = json.loads(result)
    assert parsed.get("error") == "too_many_background_subagents", parsed
    assert parsed.get("limit") == cap

    # Well under the cap: the spawn succeeds.
    for i in range(cap - 5):
        AI._BACKGROUND_SUBAGENTS.pop(f"sub-fill-{i}", None)
    result = asyncio.run(
        AI._execute_tool_call(
            "spawn_subagent",
            {"task": "fits under the cap", "background": True},
            1,
            send_func=None,
            allow_subagent=True,
        )
    )
    parsed = json.loads(result)
    assert parsed.get("ok") is True, parsed
