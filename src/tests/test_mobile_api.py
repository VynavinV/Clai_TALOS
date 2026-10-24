"""Tests for the mobile API (glasses pairing, 202 submit, event queue).

Covers the Phase-1 plan items: token create/validate/revoke with hash-only
storage, rate limit + lockout, bad token → 401, HTTP enforcement, immediate
202 with events landing in the queue (incl. speak_text derivation and the
Telegram mirror being called), size cap, long-poll cursor + ack, and the
glasses-model setting roundtrip incl. QR payload.
"""

import asyncio
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TALOS_LAZY_TOOLS", "0")

import mobile_api
import db


def _fresh_db(monkeypatch, tmp_path):
    """Isolated sqlite so token/event tests never touch real data."""
    test_db = str(tmp_path / "mobile.db")
    real_connect = sqlite3.connect

    def _connect(path=test_db):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = real_connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    monkeypatch.setattr(db, "_conn", _connect)
    db.init()


def _identity(handler):
    async def wrapper(request):
        return await handler(request)

    return wrapper


def _fake_csrf(handler):
    """Stands in for require_auth_csrf: parse the JSON body like the real one."""

    async def wrapper(request):
        try:
            request._json_body = await request.json()
        except Exception:
            request._json_body = {}
        return await handler(request)

    return wrapper


def _reset_module_state():
    mobile_api._auth_failures.clear()
    mobile_api._auth_lockouts.clear()
    mobile_api._rate_windows.clear()
    mobile_api._loop_state.clear()
    mobile_api._deps.clear()


def _make_app(env=None, write_log=None):
    from aiohttp import web

    env = dict(env or {})

    def read_env():
        return env

    def write_env(new_env):
        env.clear()
        env.update(new_env)
        if write_log is not None:
            write_log.append(dict(new_env))

    app = web.Application()
    mobile_api.setup_routes(
        app,
        require_auth=_identity,
        require_auth_csrf=_fake_csrf,
        read_env=read_env,
        write_env=write_env,
        request_origin=lambda request: "https://clai.example.ts.net",
    )
    return app


def _create_token(name="Test glasses") -> tuple[int, str]:
    import secrets

    plain = secrets.token_urlsafe(32)
    token_id = db.add_app_token(name, mobile_api._hash_token(plain))
    return token_id, plain


async def _client(app):
    from aiohttp.test_utils import TestClient, TestServer

    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def test_token_roundtrip_and_hash_only_storage(tmp_path, monkeypatch):
    _fresh_db(monkeypatch, tmp_path)
    _reset_module_state()

    token_id, plain = _create_token()
    row = db.find_app_token_by_hash(mobile_api._hash_token(plain))
    assert row is not None
    assert row["id"] == token_id
    assert row["revoked"] == 0
    # Hash-only storage: plaintext never lands in the table.
    with db._conn() as conn:
        raw = conn.execute("SELECT token_hash FROM app_tokens WHERE id = ?", (token_id,)).fetchone()
    assert raw["token_hash"] != plain
    assert raw["token_hash"] == mobile_api._hash_token(plain)

    # Revoked tokens are no longer accepted by the lookup.
    assert db.revoke_app_token(token_id) is True
    row2 = db.find_app_token_by_hash(mobile_api._hash_token(plain))
    assert row2 is not None and row2["revoked"] == 1
    assert db.revoke_app_token(token_id) is False  # already revoked


def test_status_requires_bearer_and_rejects_bad_token(tmp_path, monkeypatch):
    async def run():
        _fresh_db(monkeypatch, tmp_path)
        _reset_module_state()
        _create_token()
        app = _make_app()
        client = await _client(app)
        try:
            r = await client.get("/api/mobile/status")
            assert r.status == 401

            r = await client.get("/api/mobile/status", headers={"Authorization": "Bearer wrongtoken"})
            assert r.status == 401
        finally:
            await client.close()

    asyncio.run(run())


def test_status_ok_with_valid_token(tmp_path, monkeypatch):
    async def run():
        _fresh_db(monkeypatch, tmp_path)
        _reset_module_state()
        _, plain = _create_token()
        app = _make_app(env={"BOT_NAME": "Clai-Test", "TELEGRAM_BOT_TOKEN": "x"})
        client = await _client(app)
        try:
            r = await client.get("/api/mobile/status", headers={"Authorization": f"Bearer {plain}"})
            assert r.status == 200
            d = await r.json()
            assert d["ok"] is True
            assert d["bot_name"] == "Clai-Test"
            assert d["telegram_configured"] is True
            assert "glasses_models" in d and "Ray-Ban Meta Gen 2" in d["glasses_models"]
            # Token usage timestamp was touched.
            tokens = db.list_app_tokens()
            assert tokens[0]["last_used"] is not None
        finally:
            await client.close()

    asyncio.run(run())


def test_auth_lockout_after_repeated_failures(tmp_path, monkeypatch):
    async def run():
        _fresh_db(monkeypatch, tmp_path)
        _reset_module_state()
        _, plain = _create_token()
        app = _make_app()
        client = await _client(app)
        try:
            for _ in range(mobile_api.MAX_AUTH_FAILURES):
                r = await client.get("/api/mobile/status", headers={"Authorization": "Bearer nope"})
                assert r.status == 401
            # Even the valid token is now locked out (per-IP lockout).
            r = await client.get("/api/mobile/status", headers={"Authorization": f"Bearer {plain}"})
            assert r.status == 429
        finally:
            await client.close()

    asyncio.run(run())


def test_per_token_rate_limit(tmp_path, monkeypatch):
    async def run():
        _fresh_db(monkeypatch, tmp_path)
        _reset_module_state()
        _, plain = _create_token()
        app = _make_app(env={"MOBILE_RATE_LIMIT_PER_MIN": "3"})
        client = await _client(app)
        try:
            statuses = []
            for _ in range(5):
                r = await client.get("/api/mobile/status", headers={"Authorization": f"Bearer {plain}"})
                statuses.append(r.status)
            assert statuses[:3] == [200, 200, 200]
            assert 429 in statuses[3:]
        finally:
            await client.close()

    asyncio.run(run())


def test_https_enforcement_off_lan(tmp_path, monkeypatch):
    async def run():
        _fresh_db(monkeypatch, tmp_path)
        _reset_module_state()
        _, plain = _create_token()
        monkeypatch.setattr(mobile_api, "_is_private_remote", lambda request: False)
        app = _make_app()
        client = await _client(app)
        try:
            # aiohttp test server speaks plain http and no X-Forwarded-Proto.
            r = await client.get("/api/mobile/status", headers={"Authorization": f"Bearer {plain}"})
            assert r.status == 426

            # Opting out re-enables plain HTTP.
            app2 = _make_app(env={"MOBILE_ALLOW_HTTP": "1"})
            client2 = await _client(app2)
            try:
                r2 = await client2.get("/api/mobile/status", headers={"Authorization": f"Bearer {plain}"})
                assert r2.status == 200
            finally:
                await client2.close()

            # X-Forwarded-Proto: https (Tailscale terminate TLS in front).
            r3 = await client.get(
                "/api/mobile/status",
                headers={"Authorization": f"Bearer {plain}", "X-Forwarded-Proto": "https"},
            )
            assert r3.status == 200
        finally:
            await client.close()

    asyncio.run(run())


def test_message_size_cap(tmp_path, monkeypatch):
    async def run():
        _fresh_db(monkeypatch, tmp_path)
        _reset_module_state()
        _, plain = _create_token()
        app = _make_app(env={"MOBILE_MAX_MESSAGE_CHARS": "50"})
        client = await _client(app)
        try:
            r = await client.post(
                "/api/mobile/message",
                json={"text": "x" * 51},
                headers={"Authorization": f"Bearer {plain}"},
            )
            assert r.status == 413
            r2 = await client.post(
                "/api/mobile/message",
                json={"text": ""},
                headers={"Authorization": f"Bearer {plain}"},
            )
            assert r2.status == 400
        finally:
            await client.close()

    asyncio.run(run())


def _install_fake_core(monkeypatch, replies):
    import core

    recorded = {}

    async def fake_process_message(user_id, text, send_func, model_override=None):
        recorded["user_id"] = user_id
        recorded["text"] = text
        for reply in replies:
            await send_func(reply)

    monkeypatch.setattr(core, "process_message", fake_process_message)
    return recorded


def _insert_telegram_user(user_id=777):
    with db._conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO user_profiles (user_id, name, about) VALUES (?, ?, ?)",
            (user_id, "Test User", "about"),
        )


def test_message_flow_202_events_speak_text_and_mirror(tmp_path, monkeypatch):
    async def run():
        _fresh_db(monkeypatch, tmp_path)
        _reset_module_state()
        _insert_telegram_user(777)
        recorded = _install_fake_core(
            monkeypatch,
            [
                "Building your slideshow, this will take a few minutes.",
                "Done! Here it is: https://example.com/slides/france",
            ],
        )

        mirror_calls = []

        async def fake_mirror(text):
            mirror_calls.append(text)

        monkeypatch.setattr(mobile_api, "_make_telegram_mirror", lambda user_id: fake_mirror)

        token_id, plain = _create_token()
        app = _make_app(env={"MOBILE_REPLY_TELEGRAM": "1"})
        client = await _client(app)
        try:
            r = await client.post(
                "/api/mobile/message",
                json={"text": "build me a slideshow about France", "origin": "glasses"},
                headers={"Authorization": f"Bearer {plain}"},
            )
            assert r.status == 202
            d = await r.json()
            assert d["ok"] is True
            run_id = d["run_id"]
            assert run_id

            # Background run executes under the primary Telegram user.
            for _ in range(100):
                events = db.list_mobile_events(token_id)
                if any(e["type"] == "run_done" for e in events):
                    break
                await asyncio.sleep(0.05)
            assert recorded["user_id"] == 777
            assert recorded["text"] == "build me a slideshow about France"

            texts = [e["text"] for e in events if e["type"] == "text"]
            assert texts == [
                "Building your slideshow, this will take a few minutes.",
                "Done! Here it is: https://example.com/slides/france",
            ]
            # All glasses-origin events are tagged speak=true...
            assert all(e["speak"] == 1 for e in events if e["type"] == "text")
            # ...and speak_text is plain English: URLs replaced with a note.
            done_event = next(e for e in events if "Done!" in e["text"])
            assert "https://" not in done_event["speak_text"]
            assert "Telegram" in done_event["speak_text"]
            # Telegram mirroring was called for both text replies.
            assert mirror_calls == texts
        finally:
            await client.close()

    asyncio.run(run())


def test_events_longpoll_cursor_and_ack(tmp_path, monkeypatch):
    async def run():
        _fresh_db(monkeypatch, tmp_path)
        _reset_module_state()
        token_id, plain = _create_token()

        # Seed three events directly, then verify long-poll + cursor + ack.
        for i in range(3):
            db.add_mobile_event(token_id, "run1", "text", f"msg {i}", speak_text=f"msg {i}", speak=1)

        app = _make_app()
        client = await _client(app)
        try:
            r = await client.get(
                "/api/mobile/events?since=0&wait=1",
                headers={"Authorization": f"Bearer {plain}"},
            )
            assert r.status == 200
            d = await r.json()
            ids = [e["id"] for e in d["events"]]
            assert ids == sorted(ids)
            assert len(d["events"]) == 3
            last_id = d["last_id"]

            # Empty long-poll returns immediately with no events past cursor.
            r2 = await client.get(
                f"/api/mobile/events?since={last_id}&wait=0.2",
                headers={"Authorization": f"Bearer {plain}"},
            )
            d2 = await r2.json()
            assert d2["events"] == []

            # Ack marks delivered.
            r3 = await client.post(
                "/api/mobile/events/ack",
                json={"last_id": last_id},
                headers={"Authorization": f"Bearer {plain}"},
            )
            d3 = await r3.json()
            assert d3["ok"] is True
            assert d3["acked"] == 3

            # Late event after the cursor shows up on the next poll.
            db.add_mobile_event(token_id, "run2", "text", "late reply", speak_text="late reply", speak=1)
            await mobile_api._notify_event_waiters()
            r4 = await client.get(
                f"/api/mobile/events?since={last_id}&wait=2",
                headers={"Authorization": f"Bearer {plain}"},
            )
            d4 = await r4.json()
            assert [e["text"] for e in d4["events"]] == ["late reply"]
        finally:
            await client.close()

    asyncio.run(run())


def test_events_only_visible_to_own_token(tmp_path, monkeypatch):
    async def run():
        _fresh_db(monkeypatch, tmp_path)
        _reset_module_state()
        token_a, plain_a = _create_token("A")
        token_b, plain_b = _create_token("B")
        db.add_mobile_event(token_a, "r", "text", "secret for A", speak_text="secret for A", speak=1)

        app = _make_app()
        client = await _client(app)
        try:
            r = await client.get(
                "/api/mobile/events?since=0&wait=0.2",
                headers={"Authorization": f"Bearer {plain_b}"},
            )
            d = await r.json()
            assert d["events"] == []

            r2 = await client.get(
                "/api/mobile/events?since=0&wait=0.2",
                headers={"Authorization": f"Bearer {plain_a}"},
            )
            d2 = await r2.json()
            assert [e["text"] for e in d2["events"]] == ["secret for A"]
        finally:
            await client.close()

    asyncio.run(run())


def test_glasses_model_setting_and_qr_payload(tmp_path, monkeypatch):
    async def run():
        _fresh_db(monkeypatch, tmp_path)
        _reset_module_state()

        assert mobile_api.get_glasses_model() == mobile_api.DEFAULT_GLASSES_MODEL

        app = _make_app()
        client = await _client(app)
        try:
            # Save via the settings endpoint.
            r = await client.post(
                "/api/mobile/settings",
                json={"glasses_model": "Oakley Meta", "MOBILE_SPEAK_ENABLED": "0"},
            )
            assert r.status == 200
            d = await r.json()
            assert d["glasses_model"] == "Oakley Meta"
            assert d["speak_enabled"] is False
            assert mobile_api.get_glasses_model() == "Oakley Meta"

            # Invalid model is rejected.
            r_bad = await client.post(
                "/api/mobile/settings",
                json={"glasses_model": "Cardboard Spec"},
            )
            assert r_bad.status == 400

            # Created token QR payload embeds the model + origin + token.
            r2 = await client.post("/api/mobile/tokens", json={"name": "QR test"})
            d2 = await r2.json()
            assert d2["ok"] is True
            payload = json.loads(d2["qr_payload"])
            assert payload["m"] == "Oakley Meta"
            assert payload["u"] == "https://clai.example.ts.net"
            assert payload["t"] == d2["token"]
            assert d2["qr_url"].startswith("/api/qr?")

            # The plaintext token authenticates.
            r3 = await client.get(
                "/api/mobile/status",
                headers={"Authorization": f"Bearer {d2['token']}"},
            )
            assert r3.status == 200
        finally:
            await client.close()

    asyncio.run(run())


def test_revoke_endpoint(tmp_path, monkeypatch):
    async def run():
        _fresh_db(monkeypatch, tmp_path)
        _reset_module_state()
        app = _make_app()
        client = await _client(app)
        try:
            created = await (await client.post("/api/mobile/tokens", json={"name": "x"})).json()
            revoked = await (
                await client.post("/api/mobile/tokens/revoke", json={"id": created["id"]})
            ).json()
            assert revoked["ok"] is True
            r = await client.get(
                "/api/mobile/status",
                headers={"Authorization": f"Bearer {created['token']}"},
            )
            assert r.status == 401
            missing = await (
                await client.post("/api/mobile/tokens/revoke", json={"id": 99999})
            ).json()
            assert "error" in missing
        finally:
            await client.close()

    asyncio.run(run())


def test_speak_text_derivation():
    out = mobile_api.derive_speak_text("text", "Check this out: https://example.com/a?b=c done")
    assert "https://" not in out
    assert "Telegram" in out

    code = "Here is code:\n```python\nprint('hi')\n```\nand `inline` too"
    out2 = mobile_api.derive_speak_text("text", code, max_chars=100)
    assert "print" not in out2
    assert "inline" not in out2

    long = "word " * 200
    out3 = mobile_api.derive_speak_text("text", long, max_chars=50)
    assert len(out3) <= 51
    assert out3.endswith("…")

    assert mobile_api.derive_speak_text("document", "Slideshow about France") == "Slideshow about France"


def test_event_pruning(tmp_path, monkeypatch):
    _fresh_db(monkeypatch, tmp_path)
    _reset_module_state()
    token_id, _ = _create_token()
    db.add_mobile_event(token_id, "r", "text", "old", speak_text="old", speak=1)
    # Backdate the event beyond retention.
    with db._conn() as conn:
        conn.execute(
            "UPDATE mobile_events SET created_at = datetime('now', '-30 days')"
        )
    mobile_api._env_int = lambda key, default: 7  # force retention to 7 days
    mobile_api._enqueue_event(token_id, "r", "text", "new", speak=True)
    events = db.list_mobile_events(token_id)
    assert [e["text"] for e in events] == ["new"]
