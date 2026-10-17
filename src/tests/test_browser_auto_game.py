"""Integration tests for browser_auto's game loop: arrow keys + screenshots."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import browser_automation
import laya_decisions


class _FakeLocator:
    text = "SCORE 24 BEST 512"

    async def inner_text(self):
        return _FakeLocator.text


class _FakePage:
    url = "https://play2048.co/"
    screenshots: list[str] = []
    canvas = {"count": 0, "largest": None}

    async def title(self):
        return "2048 Game"

    def locator(self, selector):
        return _FakeLocator()

    async def evaluate(self, script):
        if "querySelectorAll('canvas')" in script:
            return _FakePage.canvas
        return []

    async def wait_for_load_state(self, *args, **kwargs):
        return None

    async def screenshot(self, path=None):
        Path(path).write_bytes(b"\x89PNG-fake")
        _FakePage.screenshots.append(path)
        return {"path": path}


class _FakeSession:
    def __init__(self, page):
        self.page = page
        self.endpoint = "fake"


def _game_harness(monkeypatch, actions: list[str]):
    """Wire browser_auto to a fake page and scripted decisions; return result."""
    page = _FakePage()
    session = _FakeSession(page)
    monkeypatch.setattr(browser_automation, "_session", session)
    monkeypatch.setattr(laya_decisions, "feature_enabled", lambda f: True)

    async def fake_availability(force=False):
        return {"available": True, "provider": "local", "reason": "ok", "detail": ""}

    monkeypatch.setattr(laya_decisions, "availability", fake_availability)

    calls = {"n": 0}
    states = []

    async def fake_try_decide(state, questions, feature=None):
        states.append(state)
        i = calls["n"]
        calls["n"] += 1
        action = actions[i] if i < len(actions) else "done"
        return {
            "answers": {
                "next_action": {"choice": action, "confidence": 0.9},
                "goal_complete": {"noul": 0.0},
            }
        }

    monkeypatch.setattr(laya_decisions, "try_decide", fake_try_decide)
    return calls, states


def test_browser_auto_plays_with_arrow_keys(monkeypatch, tmp_path):
    actions = ["press_right", "press_up", "press_left", "press_up", "press_right", "done"]
    calls, _states = _game_harness(monkeypatch, actions)

    async def run():
        return await browser_automation.browser_auto(
            goal="play 2048 and score as high as possible",
            max_steps=10,
            screenshot_every=2,
            auto_connect=False,
        )

    result = asyncio.run(run())
    assert result["status"] == "completed", result
    assert calls["n"] == len(actions)

    # Arrow-key presses became key-press steps in the transcript.
    moves = [e["action"] for e in result["transcript"] if e.get("action", "").startswith("press_")]
    assert moves == ["press_right", "press_up", "press_left", "press_up", "press_right"]

    # Screenshots captured every 2 executed steps (steps 2 and 4).
    assert len(result["screenshots"]) == 2
    for shot in result["screenshots"]:
        assert Path(shot).exists()
        assert Path(shot).read_bytes() == b"\x89PNG-fake"

    # Every 4th-move style entry in the transcript carries its screenshot path.
    shot_entries = [e for e in result["transcript"] if "screenshot" in e]
    assert [e["step"] for e in shot_entries] == [2, 4]


def test_browser_auto_flags_canvas_games_in_state(monkeypatch):
    """Canvas pages must tell the model the board is invisible — play by keyboard."""
    _FakePage.canvas = {"count": 1, "largest": {"w": 500, "h": 500}}
    try:
        calls, states = _game_harness(monkeypatch, ["press_up", "press_left", "done"])

        async def run():
            return await browser_automation.browser_auto(
                goal="play 2048 on the canvas board",
                max_steps=5,
                auto_connect=False,
            )

        result = asyncio.run(run())
        assert result["status"] == "completed"
        state = states[0]
        assert state["page_kind"] == "canvas_app"
        assert state["canvas"] == {"count": 1, "largest": {"w": 500, "h": 500}}
        assert "NOT visible" in state["canvas_note"]
        assert "arrow keys" in state["canvas_note"]
        # Score text from the DOM is the model's feedback signal.
        assert state["page_text"] == "SCORE 24 BEST 512"
    finally:
        _FakePage.canvas = {"count": 0, "largest": None}


def test_browser_auto_states_empty_text_explicitly(monkeypatch):
    """A page whose text cannot be read says so instead of showing nothing."""
    _FakeLocator.text = ""

    class _BrokenLocator:
        async def inner_text(self):
            raise RuntimeError("detached frame")

    original_locator = _FakePage.locator
    _FakePage.locator = lambda self, selector: _BrokenLocator()
    try:
        _game_harness(monkeypatch, ["done"])

        async def run():
            return await browser_automation.browser_auto(
                goal="look around",
                max_steps=2,
                auto_connect=False,
            )

        result = asyncio.run(run())
        assert result["status"] == "completed"
    finally:
        _FakePage.locator = original_locator
        _FakeLocator.text = "SCORE 24 BEST 512"


def test_browser_auto_rejects_unknown_action(monkeypatch):
    _game_harness(monkeypatch, ["teleport"])

    async def run():
        return await browser_automation.browser_auto(
            goal="do something impossible",
            max_steps=3,
            auto_connect=False,
        )

    result = asyncio.run(run())
    assert result["status"] == "error"
    assert "Invalid action" in result["transcript"][0]["error"]


def test_browser_auto_stops_on_repeated_identical_moves(monkeypatch):
    _game_harness(monkeypatch, ["press_left"] * 10)

    async def run():
        return await browser_automation.browser_auto(
            goal="play 2048",
            max_steps=10,
            auto_connect=False,
        )

    result = asyncio.run(run())
    assert result["status"] == "stuck"
    assert "repeated" in result["transcript"][-1].get("error", "").lower()
