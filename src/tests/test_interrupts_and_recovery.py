"""Tests for the hard-stop recovery watchdog, user message interrupts, and
panel confirms that can be answered from chat."""

import asyncio
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TALOS_LAZY_TOOLS", "0")

import core
import panel_popups


# ---------------------------------------------------------------------------
# Stuck / recovery settings
# ---------------------------------------------------------------------------

def test_stuck_settings_recovery_pause_default():
    cfg = core._stuck_settings()
    assert cfg["recovery_pause_s"] >= 0
    assert cfg["threshold_s"] >= 30
    assert cfg["auto_retries"] >= 0
    assert cfg["enabled"] is True


# ---------------------------------------------------------------------------
# Watchdog hard stop
# ---------------------------------------------------------------------------

def _watchdog_state(run_task=None, retries_left=1, **over):
    now = time.monotonic()
    state = {
        "started_at": now,
        "last_activity": now,
        "last_real_activity": now - 999,  # stuck for 999s
        "interventions_sent": 0,
        "retries_left": retries_left,
        "run_task": run_task,
        "cancelled_by_watchdog": False,
    }
    state.update(over)
    return state


def _fast_watchdog_cfg(**over):
    cfg = {
        "enabled": True,
        "check_interval_s": 0.05,
        "threshold_s": 30,
        "max_interventions": 2,
        "cancel_after_s": 600,
        "auto_retries": 1,
        "recovery_pause_s": 0,
    }
    cfg.update(over)
    return cfg


def test_watchdog_hard_stops_run_and_swarm(monkeypatch):
    """A stuck cancellable run with retries left is stopped completely."""
    monkeypatch.setattr(core, "_stuck_settings", lambda: _fast_watchdog_cfg())

    async def run():
        sent = []

        async def send_func(msg="", **kwargs):
            sent.append(msg)

        async def hung_turn():
            await asyncio.sleep(60)

        turn = asyncio.create_task(hung_turn())
        state = _watchdog_state(run_task=turn)

        with patch.object(core.AI, "stop_background_subagents", return_value=3) as stop:
            watchdog = asyncio.create_task(
                core._stuck_watchdog(send_func, state, None, user_id=7)
            )
            try:
                await asyncio.wait_for(asyncio.shield(turn), timeout=5)
                raise AssertionError("turn should have been cancelled")
            except asyncio.TimeoutError:
                raise AssertionError("watchdog did not cancel the turn in time")
            except asyncio.CancelledError:
                pass
            watchdog.cancel()
            try:
                await watchdog
            except asyncio.CancelledError:
                pass
            stop.assert_called_once_with(7)

        assert state["cancelled_by_watchdog"] is True
        assert state["stuck_for_s"] >= 900
        assert any("stopping the task completely" in m for m in sent)

    asyncio.run(run())


def test_watchdog_injects_when_run_not_cancellable(monkeypatch):
    """Runs without a run task (image flow) still get injected recovery."""
    monkeypatch.setattr(core, "_stuck_settings", lambda: _fast_watchdog_cfg())

    async def run():
        sent = []

        async def send_func(msg="", **kwargs):
            sent.append(msg)

        queue = asyncio.Queue(maxsize=10)
        state = _watchdog_state(run_task=None, retries_left=1)

        watchdog = asyncio.create_task(
            core._stuck_watchdog(send_func, state, queue, user_id=7)
        )
        await asyncio.sleep(0.3)
        watchdog.cancel()
        try:
            await watchdog
        except asyncio.CancelledError:
            pass

        assert not queue.empty()
        item = queue.get_nowait()
        assert item["source"] == "stuck_watchdog"
        assert "INTERVENTION" in item["text"]
        assert any("Injecting recovery" in m for m in sent)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# User message interrupts
# ---------------------------------------------------------------------------

def test_interrupt_user_run_cancels_agent_turn():
    """A message sent mid-run stops the run's agent turn and its swarm."""

    async def run():
        async def slow_turn():
            await asyncio.sleep(60)

        turn = asyncio.create_task(slow_turn())
        core._ACTIVE_AGENT_TURNS[42] = turn
        try:
            with patch.object(core.AI, "stop_background_subagents", return_value=2):
                with patch.object(core.panel_popups.hub, "dismiss_pending_confirms", return_value=0) as dismiss:
                    result = core.interrupt_user_run(42)
            assert result["run_stopped"] is True
            assert result["agents_stopped"] == 2
            dismiss.assert_called_once()
            await asyncio.wait_for(turn, timeout=5)
            raise AssertionError("turn should have been cancelled")
        except asyncio.CancelledError:
            pass
        finally:
            core._ACTIVE_AGENT_TURNS.pop(42, None)
            core._INTERRUPTED_BY_USER.discard(42)

    asyncio.run(run())


def test_interrupt_user_run_without_active_run():
    with patch.object(core.AI, "stop_background_subagents", return_value=0):
        result = core.interrupt_user_run(999)
    assert result["run_stopped"] is False
    assert result["agents_stopped"] == 0


def test_interrupted_run_persists_history_and_acks():
    """The cancelled run keeps chat history coherent and acknowledges."""

    async def run():
        sent = []

        async def send_func(msg="", **kwargs):
            sent.append(msg)

        handle = core.interrupt_hub.register(5)
        with patch.object(core.db, "add_message") as add:
            await core._acknowledge_user_interrupt(5, "original task", send_func, handle)
        core.interrupt_hub.unregister(5, handle)

        # Original message, then the marker, with the ack sent in chat.
        roles = [c.args[1] for c in add.call_args_list]
        assert roles == ["user", "assistant"]
        assert any("Stopped the previous task" in m for m in sent)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Panel confirms answered from chat
# ---------------------------------------------------------------------------

def test_confirm_answer_from_chat_text():
    hub = panel_popups.PanelPopupHub()
    hub.create_confirm(title="Deploy?", text="Ship it?", timeout_s=60, yes_label="Ship it", no_label="Hold")

    # Bare yes/no works, with surrounding whitespace/punctuation ignored.
    result = hub.answer_from_text("  Yes! ")
    assert result == {"ok": True, "answer": "yes", "via": "chat"}

    # Custom labels work, case ignored. Fresh hub per answer: a confirm is
    # single-shot.
    hub_yes = panel_popups.PanelPopupHub()
    hub_yes.create_confirm(title="x", text="y", timeout_s=60, yes_label="Ship it", no_label="Hold")
    assert hub_yes.answer_from_text("ship it")["answer"] == "yes"
    hub_no = panel_popups.PanelPopupHub()
    hub_no.create_confirm(title="x", text="y", timeout_s=60, yes_label="Ship it", no_label="Hold")
    assert hub_no.answer_from_text("HOLD")["answer"] == "no"

    # Non-answers are left alone.
    hub3 = panel_popups.PanelPopupHub()
    hub3.create_confirm(title="x", text="y", timeout_s=60)
    assert hub3.answer_from_text("actually wait, don't deploy") is None
    assert hub3.answer_from_text("") is None


def test_confirm_wait_reports_answer_source():
    hub = panel_popups.PanelPopupHub()
    popup = hub.create_confirm(title="t", text="q", timeout_s=30)

    async def run():
        wait_task = asyncio.create_task(hub.wait_for_answer(popup["id"], 5))
        await asyncio.sleep(0.05)
        hub.respond(popup["id"], "no", via="chat")
        return await wait_task

    result = asyncio.run(run())
    assert result["status"] == "answered"
    assert result["answer"] == "no"
    assert result["via"] == "chat"


def test_dismiss_pending_confirms_closes_them():
    hub = panel_popups.PanelPopupHub()
    hub.create_confirm(title="a", text="q1", timeout_s=60)
    hub.create_confirm(title="b", text="q2", timeout_s=60)
    assert hub.dismiss_pending_confirms() == 2
    assert hub.pending_list() == []
    # A later "yes" in chat must not be swallowed by the closed confirms.
    assert hub.answer_from_text("yes") is None


# ---------------------------------------------------------------------------
# Recovery retry pause
# ---------------------------------------------------------------------------

def test_recovery_pause_env_override(monkeypatch):
    monkeypatch.setenv("TALOS_STUCK_RECOVERY_PAUSE_S", "12")
    assert core._stuck_settings()["recovery_pause_s"] == 12
    monkeypatch.setenv("TALOS_STUCK_RECOVERY_PAUSE_S", "0")
    assert core._stuck_settings()["recovery_pause_s"] == 0
