"""Tests for background-service detection.

Running over SSH means the assistant has to outlive the session. start.sh now
installs a real service and records what it did in ~/.config/clai-talos/env;
these cover the parsing of that record and how each state is graded.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import diagnostics


def _with_config(contents: str | None):
    """Point _clai_config at a temporary env file (or nothing at all)."""
    tmpdir = tempfile.mkdtemp()
    home = os.path.join(tmpdir, "home")
    os.makedirs(os.path.join(home, ".config", "clai-talos"), exist_ok=True)
    if contents is not None:
        with open(os.path.join(home, ".config", "clai-talos", "env"), "w") as fh:
            fh.write(contents)

    saved = os.environ.get("HOME")
    os.environ["HOME"] = home

    def undo():
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved

    return undo


def test_config_is_empty_when_not_installed():
    undo = _with_config(None)
    try:
        assert diagnostics._clai_config() == {}
    finally:
        undo()


def test_config_parses_quoted_values():
    undo = _with_config(
        '# comment line\n'
        'INSTALL_DIR="/home/vynavin/Clai_TALOS"\n'
        'SERVICE_MANAGER="systemd"\n'
        'WEB_PORT="8080"\n'
        '\n'
    )
    try:
        config = diagnostics._clai_config()
        assert config["INSTALL_DIR"] == "/home/vynavin/Clai_TALOS"
        assert config["SERVICE_MANAGER"] == "systemd"
        assert config["WEB_PORT"] == "8080"
    finally:
        undo()


def test_comments_and_blank_lines_are_ignored():
    undo = _with_config("# only a comment\n\n   \n")
    try:
        assert diagnostics._clai_config() == {}
    finally:
        undo()


def test_missing_install_is_flagged_with_the_fix_command():
    undo = _with_config(None)
    try:
        result = asyncio.run(diagnostics.check_background_service())
        assert result.status == diagnostics.WARN
        assert "start.sh --headless" in result.hint
    finally:
        undo()


def test_launchd_install_reports_healthy():
    undo = _with_config('SERVICE_MANAGER="launchd"\n')
    try:
        result = asyncio.run(diagnostics.check_background_service())
        assert result.status == diagnostics.OK
    finally:
        undo()


def test_plain_process_is_flagged_as_not_reboot_safe():
    undo = _with_config('SERVICE_MANAGER="process"\n')
    try:
        result = asyncio.run(diagnostics.check_background_service())
        assert result.status == diagnostics.WARN
        assert "reboot" in result.detail.lower()
    finally:
        undo()


def test_unknown_manager_is_reported_rather_than_assumed_healthy():
    undo = _with_config('SERVICE_MANAGER="weird"\n')
    try:
        result = asyncio.run(diagnostics.check_background_service())
        assert result.status == diagnostics.WARN
        assert "weird" in result.detail
    finally:
        undo()


def test_enable_at_boot_repair_is_registered_and_safe():
    entry = diagnostics.REPAIRS["service.enable_boot"]
    _, destructive, label = entry
    assert destructive is False
    assert "boot" in label.lower()
