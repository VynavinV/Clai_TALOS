"""Integration tests for the agent loop's long-running machinery.

Uses a fake model_router.call_model to drive _run_agent end to end:
continuations on round exhaustion, no continuation when stalled, tool result
truncation, and interrupt delivery between rounds.
"""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TALOS_LAZY_TOOLS", "0")

import AI


class FakeModel:
    """Scripted model: emits `plan` tool-call rounds, then a final answer.

    Each call to call_model pops the next response from `script`. A script
    entry is either a list of tool calls (each a (name, args) tuple) or a
    plain string for a final text answer.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def __call__(self, model, messages, tools, **kwargs):
        self.calls += 1
        step = self.script.pop(0) if self.script else "done"
        if isinstance(step, str):
            return {
                "content": step,
                "reasoning": "",
                "tool_calls": [],
                "message": {"role": "assistant", "content": step},
            }
        tool_calls = []
        for i, (name, args) in enumerate(step):
            tool_calls.append({
                "id": f"call_{self.calls}_{i}",
                "name": name,
                "arguments": args,
            })
        return {
            "content": "",
            "reasoning": "",
            "tool_calls": tool_calls,
            "message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": __import__("json").dumps(tc["arguments"])}}
                for tc in tool_calls
            ]},
        }


async def _run(fake, rounds, continuations, prompt="do the thing"):
    with patch.object(AI.model_router, "call_model", fake):
        return await AI._run_agent(
            "test-model",
            prompt,
            system=None,
            history=None,
            tools=[],
            user_id=None,
            send_func=None,
            allow_subagent=False,
            max_rounds=rounds,
            max_calls_per_round=5,
            interrupt_event=asyncio.Event(),
            interrupt_queue=None,
            speed_mode="normal",
            reasoning_enabled=False,
            activity_state={"start": __import__("time").monotonic(), "last": __import__("time").monotonic()},
            _agent_id="test-agent",
        )


def _read_tool_round():
    # A simple, always-useful tool call the guard will not block.
    return [("save_memory", {"content": f"note {os.urandom(4).hex()}"})]


def test_continuation_extends_round_budget():
    # Model wants 3 tool rounds, budget is 2: the continuation must kick in.
    fake = FakeModel([
        _read_tool_round(),
        _read_tool_round(),
        _read_tool_round(),
        "all finished now",
    ])
    with patch.object(AI, "_MAX_TASK_CONTINUATIONS", 3):
        reply = asyncio.run(_run(fake, rounds=2, continuations=3))
    assert "finished" in reply.lower()
    assert fake.calls >= 4


def test_no_continuation_when_disabled():
    # Same setup but continuations disabled: budget exhausts after 2 rounds
    # and the wrap-up answer comes from the final tool-free call.
    fake = FakeModel([
        _read_tool_round(),
        _read_tool_round(),
        "wrap up answer",
    ])
    with patch.object(AI, "_MAX_TASK_CONTINUATIONS", 0):
        reply = asyncio.run(_run(fake, rounds=2, continuations=0))
    assert "wrap up answer" in reply


def test_stalled_run_does_not_continue():
    # Every round is an identical blocked call -> guard stalls the run; it
    # must stop rather than earning continuations forever.
    fake = FakeModel([
        [("web_search", {"query": "same query"})],
        [("web_search", {"query": "same query"})],
        [("web_search", {"query": "same query"})],
        "should not be reached",
    ])
    with patch.object(AI, "_MAX_TASK_CONTINUATIONS", 5):
        reply = asyncio.run(_run(fake, rounds=10, continuations=5))
    # The run ends through the stalled path, well before the script runs out.
    assert fake.calls < 10


def test_tool_results_truncated_in_context():
    captured = {}
    big_output = "x" * 100000

    async def fake_call(model, messages, tools, **kwargs):
        captured["messages"] = list(messages)
        step = fake_script.pop(0) if fake_script else "done"
        if isinstance(step, str):
            return {"content": step, "reasoning": "", "tool_calls": [], "message": {"role": "assistant", "content": step}}
        return {
            "content": "", "reasoning": "", "tool_calls": [{
                "id": "c1", "name": step[0][0], "arguments": step[0][1],
            }],
            "message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": step[0][0], "arguments": "{}"}}
            ]},
        }

    async def fake_tool(name, args, user_id, **kwargs):
        return f'{{"results": "{big_output}"}}'

    fake_script = [[("web_search", {"query": "big"})], "done"]

    async def run():
        with patch.object(AI.model_router, "call_model", fake_call), \
             patch.object(AI, "_execute_tool_call", fake_tool), \
             patch.object(AI, "_MAX_TOOL_RESULT_CHARS", 5000):
            return await AI._run_agent(
                "test-model", "search big", system=None, history=None,
                tools=[], user_id=None, send_func=None, allow_subagent=False,
                max_rounds=3, max_calls_per_round=5,
                interrupt_event=asyncio.Event(), interrupt_queue=None,
                speed_mode="normal", reasoning_enabled=False,
                activity_state={"start": 0, "last": 0},
            )

    asyncio.run(run())

    tool_msgs = [m for m in captured.get("messages", []) if m.get("role") == "tool"]
    assert tool_msgs, "expected a tool message in context"
    assert len(tool_msgs[0]["content"]) < 6000
    assert "[truncated" in tool_msgs[0]["content"]


def test_interrupt_delivered_between_rounds():
    # A message injected while the model works must appear in the context of
    # the next model call.
    seen = []

    async def fake_call(model, messages, tools, **kwargs):
        seen.append([str(m.get("content") or "") for m in messages])
        step = script.pop(0) if script else "done"
        if isinstance(step, str):
            return {"content": step, "reasoning": "", "tool_calls": [], "message": {"role": "assistant", "content": step}}
        return {
            "content": "", "reasoning": "", "tool_calls": [{
                "id": "c1", "name": step[0][0], "arguments": step[0][1],
            }],
            "message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": step[0][0], "arguments": "{}"}}
            ]},
        }

    async def fake_tool(name, args, user_id, **kwargs):
        # The user message arrives while this "tool" runs.
        queue.put_nowait({"text": "actually use blue", "source": "user"})
        return '{"ok": true}'

    queue = asyncio.Queue(maxsize=10)
    script = [[("web_search", {"query": "a"})], "adjusted answer"]

    async def run():
        with patch.object(AI.model_router, "call_model", fake_call), \
             patch.object(AI, "_execute_tool_call", fake_tool):
            return await AI._run_agent(
                "test-model", "paint it red", system=None, history=None,
                tools=[], user_id=None, send_func=None, allow_subagent=False,
                max_rounds=5, max_calls_per_round=5,
                interrupt_event=asyncio.Event(), interrupt_queue=queue,
                speed_mode="normal", reasoning_enabled=False,
                activity_state={"start": 0, "last": 0},
            )

    reply = asyncio.run(run())
    assert "adjusted answer" in reply
    # The second model call must have seen the interruption.
    joined = "\n".join(seen[1])
    assert "[USER INTERRUPTION]" in joined
    assert "actually use blue" in joined
