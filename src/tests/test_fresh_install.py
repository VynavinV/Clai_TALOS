"""Regression tests for defects a fresh install surfaced.

A clean install should come up clean. These cover the four things that did not:

  1. `--account None` — the None-vs-default coercion bug, this time in
     email_tools, which broke every email call the model made.
  2. The web-search probe invoked a live model, breaking the promise that the
     system check never does.
  3. .env was written under the default umask (0664) and immediately reported
     itself as insecure.
  4. The command rate limit defaulted low enough to warn on first run.
"""

import os
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import email_tools
import terminal_tools
import tool_probes


# ── 1. The "None" string bug ───────────────────────────────────────────

def test_explicit_none_does_not_become_the_string_none():
    """`str(None)` is "None", which is truthy — the root of the whole class."""
    assert email_tools._str_opt({"account": None}, "account") == ""
    assert email_tools._str_opt({}, "account") == ""
    assert email_tools._str_opt({"account": "gmail"}, "account") == "gmail"


def test_none_account_resolves_to_no_account_flag():
    """What AI.py sends for a bare request must not produce `--account None`."""
    kwargs = {"account": None}
    account = email_tools._str_opt(kwargs, "account").strip() or None
    assert account is None

    args: list[str] = []
    email_tools._add_account_arg(args, account)
    assert "None" not in args


def test_a_real_account_is_still_passed_through():
    args: list[str] = []
    email_tools._add_account_arg(args, "gmail")
    assert args == ["--account", "gmail"]


def test_none_folder_and_body_do_not_become_none_strings():
    kwargs = {"folder": None, "body": None, "subject": None, "target_folder": None}
    assert (email_tools._str_opt(kwargs, "folder").strip() or None) is None
    assert email_tools._str_opt(kwargs, "body") == ""
    assert email_tools._str_opt(kwargs, "subject") == ""
    assert email_tools._str_opt(kwargs, "target_folder").strip() == ""


def test_falsy_but_real_values_survive():
    assert email_tools._str_opt({"body": ""}, "body") == ""
    assert email_tools._str_opt({"page": 0}, "page") == "0"


# ── 2. The system check must never invoke a model ──────────────────────

def test_web_search_probe_does_not_call_the_model():
    """websearch picks a model by calling generate_content on each candidate.

    The probe must validate the credential without triggering that, or the
    "no AI model involved" guarantee is false and every check costs tokens.
    """
    import ast

    tree = ast.parse(Path(tool_probes.__file__).read_text())
    func = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "probe_web_search"
    )

    # Inspect real call sites, not the source text — the docstring legitimately
    # mentions generate_content while explaining why it is never called.
    called = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute):
            called.add(fn.attr)
        elif isinstance(fn, ast.Name):
            called.add(fn.id)

    assert "generate_content" not in called, "the probe performs an LLM inference"
    assert "call_tool" not in called, "routing through web_search triggers an inference"
    # It should still verify the credential rather than only reading the env var.
    assert "list" in called, "the probe does not actually validate the API key"


# ── 3. .env permissions ────────────────────────────────────────────────

def test_env_writer_locks_the_file_down():
    import telegram_bot

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, ".env")
        saved = telegram_bot.ENV_FILE
        telegram_bot.ENV_FILE = path
        try:
            os.umask(0o002)  # the permissive umask that produced 0664
            telegram_bot._write_env_file({"SECRET_KEY": "value"})
            mode = stat.S_IMODE(os.stat(path).st_mode)
        finally:
            telegram_bot.ENV_FILE = saved

    assert mode == 0o600, f"expected 0600, got {oct(mode)}"
    assert not (mode & 0o077), "secrets must not be group- or world-readable"


def test_env_writer_preserves_content():
    import telegram_bot

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, ".env")
        saved = telegram_bot.ENV_FILE
        telegram_bot.ENV_FILE = path
        try:
            telegram_bot._write_env_file({"A": "1", "B": "two"})
            written = telegram_bot._read_env_file()
        finally:
            telegram_bot.ENV_FILE = saved

    assert written == {"A": "1", "B": "two"}


# ── 4. Rate limit default ──────────────────────────────────────────────

def test_default_rate_limit_does_not_warn_on_a_fresh_install():
    """The diagnostics check warns below 20/min; the default must clear it."""
    executor = terminal_tools.TerminalExecutor({
        "sandbox_mode": "native", "require_confirmation": False,
    })
    assert executor.max_commands_per_minute >= 20


def test_generated_default_config_matches_the_runtime_default():
    executor = terminal_tools.TerminalExecutor({
        "sandbox_mode": "native", "require_confirmation": False,
    })
    defaults = executor._load_config()
    if "max_commands_per_minute" in defaults:
        assert defaults["max_commands_per_minute"] >= 20
