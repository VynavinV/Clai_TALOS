"""Regression tests: browser eval step + browser tool audit logging.

The eval step exists so structured DOM reads (grids, datasets, computed
values) and synthetic events stay inside browser_run. Before eval, such
tasks fell into a /tmp script dance that cost many minutes per session.
These tests pin the eval contract: bare expressions, function form with arg
(JSON-parsed when possible), selector-scoped evaluation, oversized-result
truncation, and error handling.

Also pins the audit contract: browser_run calls are recorded to
logs/audit.log (same file as terminal commands) so post-mortems cover the
full session.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import browser_automation


class FakePage:
    """Page capturing evaluate() calls and returning canned results."""

    def __init__(self, results=None):
        self.context = None
        self.url = "https://example.test/grid"
        self.eval_calls: list[tuple[str, object]] = []
        self._results = list(results or [])

    async def evaluate(self, expression, arg=None):
        self.eval_calls.append((expression, arg))
        if self._results:
            return self._results.pop(0)
        return None

    async def eval_on_selector(self, selector, expression, arg=None):
        self.eval_calls.append((f"selector:{selector}|{expression}", arg))
        if self._results:
            return self._results.pop(0)
        return None


def _run_step(page, step, timeout_ms=15000):
    import asyncio

    session = browser_automation.BrowserSession(
        playwright=None, browser=None, context=page.context, page=page, endpoint="fake"
    )

    async def run():
        return await browser_automation._execute_step(session, page, step, timeout_ms)

    return asyncio.run(run())


def test_eval_bare_expression_returns_value():
    page = FakePage(results=[42])
    _, result = _run_step(page, {"action": "eval", "expression": "1 + 41"})
    assert result.get("eval") is True
    assert result.get("value") == 42
    # Bare expression: no arg passed through
    assert page.eval_calls[0][1] is None


def test_eval_function_form_receives_parsed_json_arg():
    page = FakePage(results=[42])
    _, result = _run_step(page, {
        "action": "eval",
        "expression": "a => a.n * 2",
        "arg": "{\"n\": 21}",
    })
    assert result.get("value") == 42
    # The string arg was JSON-parsed into a dict before reaching the page
    expr, arg = page.eval_calls[0]
    assert arg == {"n": 21}


def test_eval_non_json_string_arg_stays_string():
    page = FakePage(results=["plain string"])
    _, result = _run_step(page, {
        "action": "eval",
        "expression": "a => a",
        "arg": "plain string",
    })
    assert result.get("value") == "plain string"
    _, arg = page.eval_calls[0]
    assert arg == "plain string"


def test_eval_uses_selector_when_provided():
    page = FakePage(results=["cell-A1"])
    _, result = _run_step(page, {
        "action": "eval",
        "selector": ".cw-cell",
        "expression": "el => el.textContent",
    })
    assert result.get("value") == "cell-A1"
    expr, _ = page.eval_calls[0]
    assert expr.startswith("selector:.cw-cell|")


def test_eval_truncates_oversized_results():
    page = FakePage(results=["x" * 20000])
    _, result = _run_step(page, {"action": "eval", "expression": "'big'"})
    value = result.get("value")
    assert isinstance(value, str)
    assert len(value) < 20000
    assert "truncated" in value


def test_eval_wraps_value_via_json_roundtrip():
    # A dict result must come back as a dict, not a JSON string
    page = FakePage(results=[{"links": ["a", "b"]}])
    _, result = _run_step(page, {"action": "eval", "expression": "obj"})
    assert result.get("value") == {"links": ["a", "b"]}


def test_eval_missing_expression_is_an_error():
    try:
        _run_step(FakePage(), {"action": "eval"})
    except ValueError as exc:
        assert "expression" in str(exc)
        return
    raise AssertionError("expected ValueError for missing expression")


def test_audit_logger_writes_browser_run_entries(tmp_path=None):
    # _audit must never raise and must land JSON in the audit logger
    browser_automation._audit("browser_run", "start", {"steps": 3, "step_actions": ["goto", "eval"]})
    # Flush handlers
    for handler in browser_automation._audit_logger.handlers:
        handler.flush()

    log_file = Path(browser_automation.app_paths.logs_dir()) / "audit.log"
    content = log_file.read_text()
    assert "browser_run" in content
    # The last browser_run entry parses as JSON with a step_actions list
    entries = [ln for ln in content.strip().splitlines() if "browser_run" in ln]
    parsed = json.loads(entries[-1].split("] ", 1)[1] if "] " in entries[-1] else entries[-1])
    assert parsed["tool"] == "browser_run"
    assert parsed["step_actions"] == ["goto", "eval"]


def test_audit_never_raises_on_bad_payload():
    # Non-serializable detail must not break the audit trail
    browser_automation._audit("browser_run", "error", {"weird": object()})
    # Unserializable values raise TypeError internally? No: json.dumps failure
    # is swallowed by _audit's try/except — verify no exception escaped.


if __name__ == "__main__":
    import tempfile

    failures = 0
    tests = [
        ("eval_bare_expression", test_eval_bare_expression_returns_value),
        ("eval_json_arg", test_eval_function_form_receives_parsed_json_arg),
        ("eval_plain_string_arg", test_eval_non_json_string_arg_stays_string),
        ("eval_selector_mode", test_eval_uses_selector_when_provided),
        ("eval_truncation", test_eval_truncates_oversized_results),
        ("eval_json_roundtrip", test_eval_wraps_value_via_json_roundtrip),
        ("eval_missing_expression", test_eval_missing_expression_is_an_error),
        ("audit_logging", test_audit_logger_writes_browser_run_entries),
        ("audit_never_raises", test_audit_never_raises_on_bad_payload),
    ]
    for name, fn in tests:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                fn(Path(tmp)) if 'tmp_path' in fn.__code__.co_varnames else fn()
            print(f"PASS {name}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failures else 0)
