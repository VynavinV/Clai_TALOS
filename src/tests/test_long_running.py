"""Tests for the long-running task machinery, interrupt hub, smart-home tool,
calendar delete, and the other fixes in this change set."""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TALOS_LAZY_TOOLS", "0")

import AI
import interrupt_hub
import homeassistant


# ---------------------------------------------------------------------------
# Context hygiene
# ---------------------------------------------------------------------------

def test_truncate_tool_result_keeps_short_results():
    assert AI._truncate_tool_result("short") == "short"


def test_truncate_tool_result_clamps_long_results():
    long_result = "x" * 50000
    truncated = AI._truncate_tool_result(long_result, cap=1000)
    assert len(truncated) < 2000
    assert truncated.startswith("x" * 1000)
    assert "[truncated" in truncated


def test_estimate_messages_chars_counts_content():
    msgs = [
        {"role": "system", "content": "a" * 100},
        {"role": "tool", "tool_call_id": "1", "content": "b" * 200},
    ]
    assert AI._estimate_messages_chars(msgs) >= 300


def test_compact_run_context_noop_under_threshold(monkeypatch):
    monkeypatch.setattr(AI, "_MAX_RUN_CONTEXT_CHARS", 100000)
    messages = [
        {"role": "tool", "tool_call_id": "1", "content": "x" * 5000},
    ]
    assert AI._compact_run_context(messages) is False
    assert len(messages[0]["content"]) == 5000


def test_compact_run_context_stubs_old_tool_results(monkeypatch):
    monkeypatch.setattr(AI, "_MAX_RUN_CONTEXT_CHARS", 1000)
    messages = [{"role": "system", "content": "sys"}]
    for i in range(20):
        messages.append({"role": "tool", "tool_call_id": str(i), "content": f"result-{i}-" + "y" * 900})
    changed = AI._compact_run_context(messages, keep_recent=3)
    assert changed is True
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    # The 3 most recent tool results are preserved verbatim.
    assert all(m["content"].startswith("result-") and "y" * 900 in m["content"] for m in tool_msgs[-3:])
    # Older ones are stubbed.
    assert all("compacted" in m["content"] for m in tool_msgs[:-3])
    assert all(len(m["content"]) < 700 for m in tool_msgs[:-3])


# ---------------------------------------------------------------------------
# Interrupt hub
# ---------------------------------------------------------------------------

def test_interrupt_hub_roundtrip():
    async def run():
        handle = interrupt_hub.register(1)
        try:
            assert interrupt_hub.active_handle(1) is handle
            assert interrupt_hub.inject_message(1, "hello are you there") is True
            assert interrupt_hub.inject_message(1, "") is False
            item = handle.queue.get_nowait()
            assert item == {"text": "hello are you there", "source": "user"}
            taken = interrupt_hub.take_injected(1, handle.queue)
            assert taken == ["hello are you there"]
            # Second take returns nothing (already consumed).
            assert interrupt_hub.take_injected(1, handle.queue) == []
        finally:
            interrupt_hub.unregister(1, handle)
        assert interrupt_hub.active_handle(1) is None
        assert interrupt_hub.inject_message(1, "no receiver") is False

    asyncio.run(run())


def test_take_injected_requires_matching_queue():
    async def run():
        handle = interrupt_hub.register(2)
        other_queue = asyncio.Queue()
        try:
            interrupt_hub.inject_message(2, "for the running agent")
            # A different queue must not steal the messages.
            assert interrupt_hub.take_injected(2, other_queue) == []
            assert interrupt_hub.take_injected(2, None) == []
            assert interrupt_hub.take_injected(2, handle.queue) == ["for the running agent"]
        finally:
            interrupt_hub.unregister(2, handle)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Home Assistant agent helpers
# ---------------------------------------------------------------------------

def _ha_states_body():
    return [
        {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen Light", "brightness": 200}},
        {"entity_id": "sensor.temp", "state": "21.5", "attributes": {"friendly_name": "Temp", "unit_of_measurement": "°C"}},
        {"entity_id": "cover.garage", "state": "closed", "attributes": {"friendly_name": "Garage"}},
    ]


def test_list_entities_compact_lines(monkeypatch):
    async def fake_request(method, path, json_body=None):
        assert path == "/api/states"
        return 200, _ha_states_body(), None

    monkeypatch.setattr(homeassistant, "_request", fake_request)
    monkeypatch.setattr(homeassistant, "is_configured", lambda: True)
    monkeypatch.setattr(homeassistant, "is_enabled", lambda: True)

    result = asyncio.run(homeassistant.list_entities_compact())
    assert result["ok"] is True
    assert result["total_matches"] == 3
    lines = result["entities"].split("\n")
    assert any(l.startswith("light.kitchen = on") for l in lines)
    assert any("sensor.temp = 21.5" in l and "°C" in l for l in lines)


def test_list_entities_compact_filters(monkeypatch):
    async def fake_request(method, path, json_body=None):
        return 200, _ha_states_body(), None

    monkeypatch.setattr(homeassistant, "_request", fake_request)
    monkeypatch.setattr(homeassistant, "is_configured", lambda: True)
    monkeypatch.setattr(homeassistant, "is_enabled", lambda: True)

    result = asyncio.run(homeassistant.list_entities_compact(domain="light"))
    assert result["total_matches"] == 1
    assert "light.kitchen" in result["entities"]

    result = asyncio.run(homeassistant.list_entities_compact(search="garage"))
    assert result["total_matches"] == 1
    assert "cover.garage" in result["entities"]


def test_get_entity_state(monkeypatch):
    async def fake_request(method, path, json_body=None):
        assert path == "/api/states/light.kitchen"
        return 200, {"entity_id": "light.kitchen", "state": "on", "attributes": {"brightness": 200}}, None

    monkeypatch.setattr(homeassistant, "_request", fake_request)
    monkeypatch.setattr(homeassistant, "is_configured", lambda: True)
    monkeypatch.setattr(homeassistant, "is_enabled", lambda: True)

    result = asyncio.run(homeassistant.get_entity_state("light.kitchen"))
    assert result["ok"] is True
    assert result["state"] == "on"
    assert result["attributes"]["brightness"] == 200


def test_homeassistant_execute_tool_registered():
    tools = AI._get_all_tools(include_subagent=True, include_telegram=False)
    names = {t["function"]["name"] for t in tools}
    assert "homeassistant_execute" in names
    assert "subagent_status" in names


def test_homeassistant_category_lazy_loadable():
    assert AI._category_of_tool("homeassistant_execute", set()) == "smart_home"
    assert "smart_home" in AI._TOOL_CATEGORIES


# ---------------------------------------------------------------------------
# Calendar delete
# ---------------------------------------------------------------------------

def test_calendar_delete_event_calls_delete():
    async def fake_api_call(method, path, body=None, params=""):
        assert method == "DELETE"
        assert path == "/calendar/v3/calendars/primary/events/abc123"
        return {"ok": True, "data": {}}

    import google_integration

    with patch.object(google_integration, "_google_api_call", fake_api_call):
        result = asyncio.run(
            google_integration._direct_api_fallback(
                "calendar.delete_event", {"event_id": "abc123"}
            )
        )
    assert result.get("ok") is True
    assert result.get("deleted") is True


def test_calendar_delete_requires_event_id():
    import google_integration

    result = asyncio.run(google_integration._direct_api_fallback("calendar.delete_event", {}))
    assert "error" in result


def test_calendar_update_uses_patch():
    async def fake_api_call(method, path, body=None, params=""):
        assert method == "PATCH"
        assert path == "/calendar/v3/calendars/primary/events/ev1"
        return {"ok": True, "data": {}}

    import google_integration

    with patch.object(google_integration, "_google_api_call", fake_api_call):
        result = asyncio.run(
            google_integration._direct_api_fallback(
                "calendar.update_event", {"event_id": "ev1", "event": {"summary": "New title"}}
            )
        )
    assert result.get("ok") is True


# ---------------------------------------------------------------------------
# Model router timeout policy
# ---------------------------------------------------------------------------

def test_model_timeout_default_is_long():
    import model_router

    # The default idle window must be generous for reasoning models.
    base = max(30, min(int(os.getenv("MODEL_CALL_TIMEOUT_S", "300")), 600))
    assert model_router._model_timeout_for_speed("normal", "openai") == base


# ---------------------------------------------------------------------------
# Watchdog escalation settings
# ---------------------------------------------------------------------------

def test_stuck_settings_defaults():
    import core

    cfg = core._stuck_settings()
    assert cfg["cancel_after_s"] >= 60
    assert cfg["auto_retries"] >= 0
    assert cfg["enabled"] is True


# ---------------------------------------------------------------------------
# Progress notifier (keep the user in the loop on long tasks)
# ---------------------------------------------------------------------------

def _notifier_state(**over):
    import time as _time

    now = _time.monotonic()
    state = {
        "started_at": now - 600,          # 10 min in
        "last_activity": now,
        "last_real_activity": now,
        "last_user_facing": now - 999,     # quiet for ages
        "last_auto_update": 0.0,
        "auto_updates_sent": 0,
        "last_event_desc": "Running: npm test",
    }
    state.update(over)
    return state


def test_progress_settings_defaults():
    import core

    cfg = core._progress_settings()
    assert cfg["enabled"] is True
    assert cfg["silence_threshold_s"] >= 20
    assert cfg["min_gap_s"] >= 30
    assert cfg["max_updates"] > 0


def test_progress_notifier_sends_when_quiet():
    import time as _time

    import core

    cfg = {
        "enabled": True,
        "silence_threshold_s": 90,
        "min_gap_s": 300,
        "max_updates": 60,
    }
    state = _notifier_state()
    now = _time.monotonic()

    # Quiet for 999s (> 90 threshold) and no prior auto update -> due.
    assert core._progress_due(state, cfg, now) is True

    # After sending, the state is updated like the loop does; not due again
    # until the min gap passes.
    state["auto_updates_sent"] = 1
    state["last_auto_update"] = now
    state["last_user_facing"] = now
    assert core._progress_due(state, cfg, now) is False
    assert core._progress_due(state, cfg, now + 301) is True


def test_progress_notifier_stays_quiet_after_user_message():
    import time as _time

    import core

    cfg = {"enabled": True, "silence_threshold_s": 90, "min_gap_s": 300, "max_updates": 60}
    state = _notifier_state(last_user_facing=_time.monotonic() - 10)  # messaged 10s ago
    assert core._progress_due(state, cfg, _time.monotonic()) is False


def test_progress_notifier_respects_max_updates():
    import time as _time

    import core

    cfg = {"enabled": True, "silence_threshold_s": 90, "min_gap_s": 30, "max_updates": 1}
    state = _notifier_state(auto_updates_sent=1, last_auto_update=_time.monotonic() - 999)
    assert core._progress_due(state, cfg, _time.monotonic()) is False


def test_progress_notifier_disabled():
    import core

    cfg = {"enabled": False, "silence_threshold_s": 90, "min_gap_s": 300, "max_updates": 60}
    state = _notifier_state()
    assert core._progress_due(state, cfg, 0.0) is False

    cfg2 = {"enabled": True, "silence_threshold_s": 90, "min_gap_s": 300, "max_updates": 0}
    assert core._progress_due(state, cfg2, 0.0) is False


def test_summarize_event_truncates():
    import core

    evt = {"type": "command", "detail": "x" * 500, "label": "Command"}
    out = core._summarize_event(evt)
    assert len(out) <= 110
    assert out.endswith("...")


# ---------------------------------------------------------------------------
# Messaging budget scaling
# ---------------------------------------------------------------------------

def test_loop_guard_grants_extra_messages():
    guard = AI._LoopGuard(is_orchestrator=True)
    base = guard._max_telegram
    guard.grant_messages(2)
    assert guard._max_telegram == base + 2
    guard.grant_messages(-5)  # negative is ignored
    assert guard._max_telegram == base + 2


def test_subagent_send_func_long_running_budget():
    short_budget = AI._subagent_message_budget(long_running=False)
    long_budget = AI._subagent_message_budget(long_running=True)

    short_max, short_interval, _ = short_budget
    long_max, long_interval, _ = long_budget

    # Long-running subagents get a clearly larger update budget so a
    # multi-hour subagent can post milestone updates instead of going dark.
    assert long_max >= 8
    assert long_max > short_max
    assert short_interval > 0 and long_interval > 0


def test_subagent_send_func_wrapper_uses_budget():
    sent = []

    async def send_func(message="", **kwargs):
        sent.append(message)

    wrapper = AI._build_subagent_send_func(send_func, long_running=False)
    assert wrapper is not None

    async def run():
        # One send goes through immediately; rapid duplicates are throttled
        # by the interval, which is the intended anti-spam behavior.
        await wrapper("starting work")

    asyncio.run(run())
    assert sent == ["starting work"]
