"""Regression tests for Tailscale HTTPS setup.

Background: the dashboard only ever serves plain HTTP on a loopback port —
HTTPS comes from Tailscale terminating TLS in front of it. Nothing in setup ever
ran `tailscale serve`, so every install stayed on HTTP no matter how the tailnet
was configured.

Two parsing traps are covered here because both produced convincing false
readings during development:
  - tailscale prints a client/server version-skew warning on stderr, which
    corrupts the JSON if the streams are merged;
  - `serve status` reporting a proxy is not proof that anything is listening,
    which is exactly what happens when two tailscaled daemons are installed.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import telegram_bot


SERVE_JSON = {
    "TCP": {"443": {"HTTPS": True}},
    "Web": {"host.tail1234.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8080"}}}},
}


def _patched(monkey: dict):
    """Apply attribute overrides to telegram_bot, returning an undo callable."""
    saved = {k: getattr(telegram_bot, k) for k in monkey}
    for k, v in monkey.items():
        setattr(telegram_bot, k, v)
    return lambda: [setattr(telegram_bot, k, v) for k, v in saved.items()]


def test_serve_config_survives_the_version_warning_on_stderr():
    """The warning goes to stderr, so it must not reach the JSON parser."""
    undo = _patched({
        "_resolve_tailscale_bin": lambda: "/usr/bin/tailscale",
        "_run_tailscale": lambda args, timeout=20: (0, json.dumps(SERVE_JSON), 'Warning: client version "1.96.4" != tailscaled server version "1.96.5"'),
    })
    try:
        assert telegram_bot.get_serve_config() == SERVE_JSON
    finally:
        undo()


def test_serve_config_ignores_trailing_output_after_the_json():
    """raw_decode stops at the end of the object instead of choking."""
    noisy = json.dumps(SERVE_JSON) + "\nWarning: something happened afterwards\n"
    undo = _patched({
        "_resolve_tailscale_bin": lambda: "/usr/bin/tailscale",
        "_run_tailscale": lambda args, timeout=20: (0, noisy, ""),
    })
    try:
        assert telegram_bot.get_serve_config() == SERVE_JSON
    finally:
        undo()


def test_serve_config_is_empty_when_nothing_is_configured():
    undo = _patched({
        "_resolve_tailscale_bin": lambda: "/usr/bin/tailscale",
        "_run_tailscale": lambda args, timeout=20: (0, "{}", ""),
    })
    try:
        assert telegram_bot.get_serve_config() == {}
    finally:
        undo()


def test_unconfigured_state_is_reported_as_fixable():
    undo = _patched({
        "_resolve_tailscale_bin": lambda: "/usr/bin/tailscale",
        "get_serve_config": lambda: {},
    })
    try:
        state = telegram_bot.tailscale_https_state()
        assert state["ok"] is False
        assert state["configured"] is False
        assert "http only" in state["detail"].lower()
    finally:
        undo()


def test_configured_and_reachable_is_ok():
    undo = _patched({
        "_resolve_tailscale_bin": lambda: "/usr/bin/tailscale",
        "get_serve_config": lambda: SERVE_JSON,
        "probe_https_port": lambda timeout=4.0: (True, "100.0.0.1:443 accepting connections"),
        "get_tailscale_hostname": lambda: "host.tail1234.ts.net",
    })
    try:
        state = telegram_bot.tailscale_https_state()
        assert state["ok"] is True
        assert state["mode"] == "serve"
        assert state["url"] == "https://host.tail1234.ts.net"
    finally:
        undo()


def test_configured_but_unreachable_is_not_reported_as_ok():
    """Config alone must never count as success — the port has to answer."""
    undo = _patched({
        "_resolve_tailscale_bin": lambda: "/usr/bin/tailscale",
        "get_serve_config": lambda: SERVE_JSON,
        "probe_https_port": lambda timeout=4.0: (False, "100.0.0.1:443 unreachable"),
        "_count_tailscale_daemons": lambda: 1,
        "get_tailscale_hostname": lambda: "host.tail1234.ts.net",
    })
    try:
        state = telegram_bot.tailscale_https_state()
        assert state["ok"] is False
        assert state["configured"] is True
        assert state["reachable"] is False
    finally:
        undo()


def test_duplicate_daemons_are_called_out_by_name():
    undo = _patched({
        "_resolve_tailscale_bin": lambda: "/usr/bin/tailscale",
        "get_serve_config": lambda: SERVE_JSON,
        "probe_https_port": lambda timeout=4.0: (False, "100.0.0.1:443 unreachable"),
        "_count_tailscale_daemons": lambda: 2,
        "get_tailscale_hostname": lambda: "host.tail1234.ts.net",
    })
    try:
        state = telegram_bot.tailscale_https_state()
        assert state["ok"] is False
        assert state["daemons"] == 2
        assert "daemon" in state["detail"].lower()
    finally:
        undo()


def test_funnel_mode_is_distinguished_from_serve():
    funnel_json = dict(SERVE_JSON, AllowFunnel={"host.tail1234.ts.net:443": True})
    undo = _patched({
        "_resolve_tailscale_bin": lambda: "/usr/bin/tailscale",
        "get_serve_config": lambda: funnel_json,
        "probe_https_port": lambda timeout=4.0: (True, "ok"),
        "get_tailscale_hostname": lambda: "host.tail1234.ts.net",
    })
    try:
        state = telegram_bot.tailscale_https_state()
        assert state["ok"] is True
        assert state["mode"] == "funnel"
    finally:
        undo()


def test_a_proxy_to_a_different_port_does_not_count():
    other = {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {"host:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9999"}}}},
    }
    undo = _patched({
        "_resolve_tailscale_bin": lambda: "/usr/bin/tailscale",
        "get_serve_config": lambda: other,
    })
    try:
        state = telegram_bot.tailscale_https_state()
        assert state["ok"] is False
        assert str(telegram_bot.WEB_PORT) in state["detail"]
    finally:
        undo()


def test_https_mode_defaults_to_tailnet_only():
    """Funnel publishes to the whole internet; setup must not choose it silently."""
    assert telegram_bot.TAILSCALE_HTTPS_MODE in {"serve", "funnel", "off"}
    assert telegram_bot.TAILSCALE_HTTPS_MODE == "serve"
