"""Tests for repair coverage and the Fix All plan.

Two things matter here:

  1. Every check that can report a problem should offer a way out, so the
     dashboard is not a list of things the user has to go and fix by hand.
  2. The Fix All confirmation must describe exactly what the server will do.
     The dashboard builds its preview in JavaScript while `run_auto_repair`
     decides independently in Python; if those two ever disagree the dialog is
     lying about what is about to happen.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import diagnostics


def _repair_ids() -> set[str]:
    return set(diagnostics.REPAIRS)


def test_every_referenced_repair_exists():
    """A check pointing at a repair id that is not registered is a dead button."""
    import inspect

    referenced = set()
    for source in (
        inspect.getsource(diagnostics),
        inspect.getsource(sys.modules[diagnostics.tool_probes.__name__]),
    ):
        referenced |= set(re.findall(r'fix="([a-z_]+(?:\.[a-z_]+)+)"', source))
        referenced |= set(re.findall(r'reset="([a-z_]+(?:\.[a-z_]+)+)"', source))

    missing = sorted(referenced - _repair_ids())
    assert not missing, f"checks reference repairs that do not exist: {missing}"


def test_every_repair_is_reachable_from_some_check():
    """An unreferenced repair can never be triggered from the wizard."""
    import inspect

    referenced = set()
    for source in (
        inspect.getsource(diagnostics),
        inspect.getsource(sys.modules[diagnostics.tool_probes.__name__]),
    ):
        referenced |= set(re.findall(r'fix="([a-z_]+(?:\.[a-z_]+)+)"', source))
        referenced |= set(re.findall(r'reset="([a-z_]+(?:\.[a-z_]+)+)"', source))

    # These are driven by buttons rather than by an individual check.
    button_driven = {"system.full_reset"}
    orphans = sorted(_repair_ids() - referenced - button_driven)
    assert not orphans, f"repairs no check can reach: {orphans}"


def test_destructive_flags_are_declared_for_every_repair():
    for repair in diagnostics.list_repairs():
        assert isinstance(repair["destructive"], bool)
        assert repair["label"], f"{repair['id']} has no human label"


def test_full_reset_is_marked_destructive():
    _, destructive, _ = diagnostics.REPAIRS["system.full_reset"]
    assert destructive is True


def test_clearing_a_credential_is_marked_destructive():
    _, destructive, _ = diagnostics.REPAIRS["web.clear_gemini_key"]
    assert destructive is True


def test_installing_the_service_is_not_destructive():
    _, destructive, _ = diagnostics.REPAIRS["service.install_background"]
    assert destructive is False


def test_full_reset_only_references_real_repairs():
    """It runs a hard-coded list; a typo there would raise at repair time."""
    import inspect

    source = inspect.getsource(diagnostics._repair_full_reset)
    ids = set(re.findall(r'"([a-z_]+\.[a-z_]+)"', source))
    missing = sorted(ids - _repair_ids())
    assert not missing, f"full reset references unknown repairs: {missing}"


def test_fix_all_preview_matches_what_the_server_will_run():
    """Parity between the dashboard's JS plan and run_auto_repair's selection.

    Both must pick `fix` first and fall back to `reset` only when no safe fix
    exists — otherwise the confirmation dialog lists actions that never happen,
    or omits ones that do.
    """
    dashboard = (Path(__file__).resolve().parents[1] / "web" / "dashboard.html").read_text()
    plan_start = dashboard.index("function repairPlan()")
    plan_body = dashboard[plan_start:plan_start + 1400]

    # The JS must choose one id per check, preferring fix.
    assert "c.fix || c.reset" in plan_body, \
        "repairPlan must mirror run_auto_repair's fix-then-reset preference"
    assert "[c.fix, c.reset].forEach" not in plan_body, \
        "listing both fix and reset would over-promise: the server runs only one"

    # And the server side must still work the way the JS assumes.
    server = __import__("inspect").getsource(diagnostics.run_auto_repair)
    assert 'check.get("fix") or' in server


def test_auto_repair_skips_destructive_without_confirmation():
    """The safe button must never trigger a destructive repair."""
    import inspect

    source = inspect.getsource(diagnostics.run_auto_repair)
    assert "if confirm_destructive else None" in source
