"""Regression tests for the Himalaya version guard.

Background: the installer downloaded `releases/latest` while email_tools was
written against the 1.2.x CLI. When upstream shipped the 1.3 rework the flags
and subcommands changed, every email call started failing with
"unexpected argument '--output'", and nothing in the system noticed — the
onboarding wizard's own verification step was the one Himalaya invocation that
did not exercise the broken arguments.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import email_tools


def test_parses_himalaya_version_banner():
    banner = "himalaya v1.2.0 +smtp +imap +sendmail +maildir +wizard +pgp-commands"
    assert email_tools._parse_version(banner) == (1, 2, 0)


def test_parses_version_without_v_prefix():
    assert email_tools._parse_version("himalaya 1.2.3") == (1, 2, 3)


def test_unparseable_banner_returns_none():
    assert email_tools._parse_version("not a version string") is None


def test_target_version_is_supported():
    target = tuple(int(p) for p in email_tools.HIMALAYA_TARGET_VERSION.split("."))
    assert email_tools._version_supported(target)


def test_rejects_the_cli_rework_that_broke_email():
    # 1.3 renamed --output to --json and moved composition out to mml.
    assert not email_tools._version_supported((1, 3, 0))


def test_rejects_older_and_unknown_versions():
    assert not email_tools._version_supported((0, 9, 0))
    assert not email_tools._version_supported((2, 0, 0))
    assert not email_tools._version_supported(None)


def test_missing_config_fails_fast_without_spawning():
    """No config must produce an actionable error, not Himalaya's TTY wizard crash."""
    import os

    saved = os.environ.get("HIMALAYA_CONFIG")
    os.environ["HIMALAYA_CONFIG"] = ""
    try:
        result = asyncio.run(email_tools.execute("list_folders"))
    finally:
        if saved is None:
            os.environ.pop("HIMALAYA_CONFIG", None)
        else:
            os.environ["HIMALAYA_CONFIG"] = saved

    assert result["ok"] is False
    assert result.get("needs_setup") is True
    assert "not configured" in result["error"].lower()
    # The old failure mode leaked Himalaya's wizard error instead.
    assert "tty" not in result["error"].lower()


def test_friendly_error_explains_argument_rejection():
    message = email_tools._friendly_error("error: unexpected argument '--output' found", "")
    assert email_tools.HIMALAYA_TARGET_VERSION in message
    assert "version" in message.lower()


def test_friendly_error_explains_the_tty_wizard_crash():
    raw = '{"error":"cannot prompt boolean","sources":["The input device is not a TTY"]}'
    message = email_tools._friendly_error(raw, "")
    assert "config" in message.lower()
    assert "wizard" in message.lower()
