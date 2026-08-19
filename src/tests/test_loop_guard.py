"""Regression tests for agent loop stall detection.

Background: the stall detector only counted a round as stalled when the repeat
guard blocked every call. A call that ran and returned an error — a saturated
rate limiter, a broken CLI — counted as progress and reset the counter, so a
jammed backend let the agent run to its full round cap retrying something that
could never succeed.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import AI


def test_rate_limited_result_counts_as_no_progress():
    payload = json.dumps({
        "error": "Rate limit exceeded: at most 10 commands per minute.",
        "exit_code": -1,
        "rate_limited": True,
        "retry_after_s": 42,
    })
    assert AI._tool_result_is_failure(payload)


def test_guard_block_counts_as_no_progress():
    assert AI._tool_result_is_failure(json.dumps({"error": "repeated_tool_call"}))


def test_duplicate_message_block_counts_as_no_progress():
    assert AI._tool_result_is_failure(json.dumps({"sent": False, "blocked": "duplicate_message"}))


def test_tool_level_failure_counts_as_no_progress():
    assert AI._tool_result_is_failure(json.dumps({"ok": False, "error": "version mismatch"}))


def test_empty_result_counts_as_no_progress():
    assert AI._tool_result_is_failure("")


def test_successful_result_counts_as_progress():
    assert not AI._tool_result_is_failure(json.dumps({"ok": True, "folders": ["INBOX"]}))


def test_plain_text_output_counts_as_progress():
    assert not AI._tool_result_is_failure("INBOX\nSent\nDrafts")


def test_json_array_counts_as_progress():
    assert not AI._tool_result_is_failure(json.dumps([{"id": 1}, {"id": 2}]))


def test_stall_trips_after_consecutive_failed_rounds():
    guard = AI._LoopGuard(is_orchestrator=True)
    # MAX_STALLED_ROUNDS defaults to 2, so the second failed round should stop it.
    assert guard.note_round(True) in (False, True)
    stopped = guard.note_round(True)
    assert stopped is True


def test_a_good_round_resets_the_stall_counter():
    guard = AI._LoopGuard(is_orchestrator=True)
    guard.note_round(True)
    guard.note_round(False)
    assert guard.stalled_rounds == 0
