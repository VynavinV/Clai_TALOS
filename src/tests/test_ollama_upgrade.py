"""Tests for the streamed Ollama self-upgrade (Linux path) and op progress."""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ollama_setup


class _FakeStdout:
    def __init__(self, lines: list[bytes]):
        self._lines = list(lines)

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        return b""


class _FakeProc:
    def __init__(self, lines: list[bytes], exit_code: int = 0):
        self.stdout = _FakeStdout(lines)
        self._exit_code = exit_code
        self.returncode = None
        self.killed = False

    async def wait(self):
        self.returncode = self._exit_code if not self.killed else -9
        return self.returncode

    def kill(self):
        self.killed = True


async def _consume(gen):
    return [event async for event in gen]


def test_linux_upgrade_streams_installer_lines(monkeypatch):
    """Installer output flows through as live progress events with elapsed time."""
    monkeypatch.setattr(ollama_setup.sys, "platform", "linux")
    monkeypatch.setattr(ollama_setup.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(
        ollama_setup.asyncio,
        "create_subprocess_shell",
        lambda cmd, **kwargs: _async_result(
            _FakeProc([
                b">>> Downloading ollama...\n",
                b">>> Installing ollama to /usr/lib/ollama\n",
            ])
        ),
    )

    async def fake_wait(native_root, timeout_s=90.0):
        return True, "0.42.1"

    monkeypatch.setattr(ollama_setup, "_wait_server_version", fake_wait)

    events = asyncio.run(_consume(ollama_setup.upgrade_ollama()))
    messages = [e.get("message", "") for e in events]
    assert any("Downloading ollama" in m for m in messages)
    assert any("Installing ollama" in m for m in messages)
    # elapsed is attached so a slow download never looks stuck
    progress_events = [e for e in events if e.get("stage") == "progress" and "elapsed_s" in e]
    assert progress_events
    done = [e for e in events if e.get("stage") == "done"]
    assert done and "0.42.1" in done[0]["message"]


def _async_result(value):
    future = asyncio.get_event_loop().create_future() if False else None

    class _Awaitable:
        def __await__(self):
            return self._coro().__await__()

        async def _coro(self):
            return value

    return _Awaitable()


def test_linux_upgrade_requires_root_or_sudo(monkeypatch):
    monkeypatch.setattr(ollama_setup.sys, "platform", "linux")
    monkeypatch.setattr(ollama_setup.os, "geteuid", lambda: 1000, raising=False)

    def fake_run_shell(cmd, timeout):
        return 1, "sudo: a password is required"

    monkeypatch.setattr(ollama_setup, "_run_shell", fake_run_shell)

    events = asyncio.run(_consume(ollama_setup.upgrade_ollama()))
    errors = [e for e in events if e.get("stage") == "error"]
    assert errors
    assert "needs root" in errors[0]["error"]


def test_linux_upgrade_reports_installer_failure(monkeypatch):
    monkeypatch.setattr(ollama_setup.sys, "platform", "linux")
    monkeypatch.setattr(ollama_setup.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(
        ollama_setup.asyncio,
        "create_subprocess_shell",
        lambda cmd, **kwargs: _async_result(
            _FakeProc([b">>> Downloading ollama...\n", b"curl: (28) timed out\n"], exit_code=1)
        ),
    )

    events = asyncio.run(_consume(ollama_setup.upgrade_ollama()))
    errors = [e for e in events if e.get("stage") == "error"]
    assert errors
    assert "exit 1" in errors[0]["error"]
    assert "timed out" in errors[0]["error"]


def test_linux_upgrade_reports_wrong_version(monkeypatch):
    """A server that comes back still old is an error, not a silent success."""
    monkeypatch.setattr(ollama_setup.sys, "platform", "linux")
    monkeypatch.setattr(ollama_setup.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(
        ollama_setup.asyncio,
        "create_subprocess_shell",
        lambda cmd, **kwargs: _async_result(_FakeProc([b">>> Installing...\n"])),
    )

    async def fake_wait(native_root, timeout_s=90.0):
        return True, "0.32.14"

    monkeypatch.setattr(ollama_setup, "_wait_server_version", fake_wait)

    events = asyncio.run(_consume(ollama_setup.upgrade_ollama()))
    errors = [e for e in events if e.get("stage") == "error"]
    assert errors
    assert "0.32.14" in errors[0]["error"]


def test_claim_ollama_upgrade_prevents_double_run():
    import laya_decisions

    laya_decisions.set_operation_progress(None, done=True)
    # Nothing running: the button can claim it.
    assert laya_decisions.claim_ollama_upgrade() is True
    # Now an upgrade is active: a second claim is refused.
    laya_decisions.set_operation_progress("upgrade_ollama", message="upgrading...")
    assert laya_decisions.claim_ollama_upgrade() is False
    # The background self-healer defers to the claimed run.
    assert laya_decisions._ollama_upgrade_started is True
    laya_decisions.set_operation_progress(None, done=True)

    # Done/stale operations release the claim.
    laya_decisions._op_state["updated_at"] = laya_decisions._op_state["updated_at"] - 1900
    assert laya_decisions.claim_ollama_upgrade() is True
    laya_decisions.set_operation_progress(None, done=True)


def test_claim_laya_pull_blocks_when_active():
    import laya_decisions

    laya_decisions.set_operation_progress(None, done=True)
    assert laya_decisions.claim_laya_pull() is True
    laya_decisions.set_operation_progress("pull", percent=10, message="downloading")
    assert laya_decisions.claim_laya_pull() is False
    laya_decisions.set_operation_progress(None, done=True)
