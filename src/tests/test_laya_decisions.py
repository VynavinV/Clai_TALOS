"""Tests for the Laya / Jev decision layer and its TALOS integrations.

Covers the wire-format contract (payload building, response normalization),
provider resolution, fail-open behavior, availability probing (including the
Ollama 0.40+ version gate), feature gating, and the decision-powered
integrations (email triage digest, memory rerank, model triage, browser
auto-pilot text extraction).
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TALOS_LAZY_TOOLS", "0")

import httpx
import laya_decisions
import AI


# ---------------------------------------------------------------------------
# Fake HTTP plumbing
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeClient:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if self._exc is not None:
            raise self._exc
        return self._response


def _patch_http(monkeypatch, response=None, exc=None):
    fake = _FakeClient(response=response, exc=exc)
    monkeypatch.setattr(laya_decisions.httpx, "AsyncClient", lambda **kwargs: fake)
    return fake


# ---------------------------------------------------------------------------
# Question builders and normalization
# ---------------------------------------------------------------------------


def test_question_builders():
    assert laya_decisions.choice("Which?", {"a": "A", "b": "B"}) == {
        "type": "choice",
        "instructions": "Which?",
        "criteria": {"a": "A", "b": "B"},
    }
    assert laya_decisions.score("How much?", ["low", "high"]) == {
        "type": "score",
        "instructions": "How much?",
        "criteria": ["low", "high"],
    }
    assert laya_decisions.noul("Is it true?") == {"type": "noul", "instructions": "Is it true?"}


def test_normalize_questions_passes_valid_map():
    questions = {
        "dept": laya_decisions.choice("Which team?", {"tech": "bugs", "other": "rest"}),
        "urgent": laya_decisions.noul("Is it urgent?"),
        "sev": laya_decisions.score("How severe?", ["low", "mid", "high"]),
    }
    normalized = laya_decisions.normalize_questions(questions)
    assert normalized["dept"]["criteria"] == {"tech": "bugs", "other": "rest"}
    assert normalized["urgent"] == {"type": "noul", "instructions": "Is it urgent?"}
    assert normalized["sev"]["criteria"] == ["low", "mid", "high"]


def test_normalize_questions_rejects_bad_input():
    for bad in (
        None,
        {},
        "not a dict",
        {"q": "not an object"},
        {"q": {"type": "freeform", "instructions": "?"}},
        {"q": {"type": "choice", "instructions": "?"}},  # missing criteria
        {"q": {"type": "choice", "instructions": "?", "criteria": {}}},
        {"q": {"type": "score", "instructions": "?", "criteria": ["only one"]}},
        {"q": {"type": "noul"}},  # missing instructions
    ):
        try:
            laya_decisions.normalize_questions(bad)
            raised = False
        except laya_decisions.LayaError:
            raised = True
        assert raised, f"normalize_questions accepted {bad!r}"


def test_normalize_questions_caps_option_count():
    options = {f"o{i}": "desc" for i in range(300)}
    try:
        laya_decisions.normalize_questions({"q": laya_decisions.choice("?", options)})
        raised = False
    except laya_decisions.LayaError:
        raised = True
    assert raised


# ---------------------------------------------------------------------------
# Payload building and response extraction
# ---------------------------------------------------------------------------


def test_build_payload_keeps_structured_state():
    state = {"from": "a@x.com", "body": "hello"}
    payload = laya_decisions._build_payload(state, {"q": laya_decisions.noul("?")}, "laya")
    assert payload["state"] == state
    assert payload["model"] == "laya"
    assert payload["questions"]["q"]["type"] == "noul"


def test_build_payload_truncates_long_string_state():
    payload = laya_decisions._build_payload("x" * 60000, {"q": laya_decisions.noul("?")}, None)
    assert len(payload["state"]) == laya_decisions.MAX_STATE_CHARS
    assert "model" not in payload


def test_build_payload_rejects_huge_structured_state():
    state = {"blob": "x" * 60000}
    try:
        laya_decisions._build_payload(state, {"q": laya_decisions.noul("?")}, None)
        raised = False
    except laya_decisions.LayaError:
        raised = True
    assert raised


def test_extract_answers_normalizes_known_fields():
    data = {
        "answers": {
            "dept": {"choice": "billing", "probabilities": {"billing": 0.9}, "confidence": 0.9},
            "risk": {"noul": "0.87"},
            "sev": {"score": "2.5"},
        }
    }
    answers = laya_decisions._extract_answers(data)
    assert answers["dept"]["choice"] == "billing"
    assert answers["dept"]["confidence"] == 0.9
    assert answers["risk"]["noul"] == 0.87
    assert answers["sev"]["score"] == 2.5


def test_extract_answers_rejects_missing_payload():
    assert laya_decisions._extract_answers({"nope": True}) is None
    assert laya_decisions._extract_answers("string") is None


# ---------------------------------------------------------------------------
# decide() over the wire (mocked)
# ---------------------------------------------------------------------------


def test_decide_posts_expected_shape(monkeypatch):
    fake = _patch_http(
        monkeypatch,
        response=_FakeResponse(payload={
            "answers": {"dept": {"choice": "tech", "confidence": 0.9}}
        }),
    )
    monkeypatch.setenv("LAYA_PROVIDER", "custom")
    monkeypatch.setenv("LAYA_BASE_URL", "http://localhost:9000")

    result = asyncio.run(
        laya_decisions.decide("hello", {"dept": laya_decisions.choice("?", {"tech": "bugs"})})
    )

    assert result["answers"]["dept"]["choice"] == "tech"
    call = fake.calls[0]
    assert call["url"] == "http://localhost:9000/v1/systemone"
    assert call["json"]["state"] == "hello"
    assert call["json"]["questions"]["dept"]["type"] == "choice"
    assert call["json"]["model"] == "laya"


def test_decide_raises_on_http_error(monkeypatch):
    _pin_custom_provider(monkeypatch)
    _patch_http(monkeypatch, response=_FakeResponse(status_code=502, text="bad gateway"))
    try:
        asyncio.run(laya_decisions.decide("x", {"q": laya_decisions.noul("?")}))
        raised = False
    except laya_decisions.LayaError as exc:
        raised = True
        assert "502" in str(exc)
    assert raised


def test_decide_raises_on_connection_error(monkeypatch):
    _pin_custom_provider(monkeypatch)
    _patch_http(monkeypatch, exc=httpx.ConnectError("connection refused"))
    try:
        asyncio.run(laya_decisions.decide("x", {"q": laya_decisions.noul("?")}))
        raised = False
    except laya_decisions.LayaError:
        raised = True
    assert raised


def test_decide_raises_when_disabled(monkeypatch):
    monkeypatch.setenv("LAYA_ENABLED", "0")
    try:
        asyncio.run(laya_decisions.decide("x", {"q": laya_decisions.noul("?")}))
        raised = False
    except laya_decisions.LayaUnavailableError:
        raised = True
    assert raised


def _pin_custom_provider(monkeypatch):
    """Pin the HTTP path so decide() never probes the machine's real backends."""
    monkeypatch.setenv("LAYA_PROVIDER", "custom")
    monkeypatch.setenv("LAYA_BASE_URL", "http://localhost:9000")


def test_try_decide_fails_open_and_returns_none(monkeypatch):
    _pin_custom_provider(monkeypatch)
    _patch_http(monkeypatch, exc=httpx.ConnectError("down"))
    result = asyncio.run(laya_decisions.try_decide("x", {"q": laya_decisions.noul("?")}, feature="email_triage"))
    assert result is None


def test_try_decide_returns_result_on_success(monkeypatch):
    _pin_custom_provider(monkeypatch)
    _patch_http(monkeypatch, response=_FakeResponse(payload={"answers": {"q": {"noul": 0.5}}}))
    result = asyncio.run(laya_decisions.try_decide("x", {"q": laya_decisions.noul("?")}))
    assert result["answers"]["q"]["noul"] == 0.5


# ---------------------------------------------------------------------------
# Provider resolution
# ---------------------------------------------------------------------------


def test_provider_resolution_modes(monkeypatch):
    monkeypatch.delenv("LAYA_BASE_URL", raising=False)
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    assert laya_decisions.resolve_provider() == "auto"

    monkeypatch.setenv("LAYA_PROVIDER", "jev")
    assert laya_decisions.resolve_provider() == "jev"

    monkeypatch.setenv("LAYA_PROVIDER", "local")
    assert laya_decisions.resolve_provider() == "local"

    monkeypatch.setenv("LAYA_PROVIDER", "auto")
    monkeypatch.setenv("JEV_API_KEY", "jv_live_x")
    assert laya_decisions.resolve_provider() == "jev"

    monkeypatch.setenv("LAYA_BASE_URL", "http://localhost:8000")
    assert laya_decisions.resolve_provider() == "custom"


def test_systemone_url_handles_common_bases():
    assert laya_decisions._systemone_url("http://localhost:11434") == "http://localhost:11434/v1/systemone"
    assert laya_decisions._systemone_url("https://api.typesafe.ai/v1/") == "https://api.typesafe.ai/v1/systemone"
    assert laya_decisions._systemone_url("localhost:8000") == "http://localhost:8000/v1/systemone"
    assert laya_decisions._systemone_url("") == ""


def test_provider_endpoint_headers_and_model(monkeypatch):
    monkeypatch.setenv("LAYA_PROVIDER", "jev")
    monkeypatch.setenv("JEV_API_KEY", "jv_live_x")
    endpoint = laya_decisions.provider_endpoint()
    assert endpoint["url"] == "https://api.typesafe.ai/v1/systemone"
    assert endpoint["headers"]["Authorization"] == "Bearer jv_live_x"
    assert endpoint["model"] == "jev-1"

    monkeypatch.setenv("LAYA_PROVIDER", "ollama")
    monkeypatch.setenv("LAYA_MODEL", "laya-multilingual")
    endpoint = laya_decisions.provider_endpoint()
    assert endpoint["url"].endswith(":11434/v1/systemone")
    assert endpoint["model"] == "laya-multilingual"


# ---------------------------------------------------------------------------
# Version parsing and availability
# ---------------------------------------------------------------------------


def test_parse_version():
    assert laya_decisions._parse_version("0.33.2") == (0, 33, 2)
    assert laya_decisions._parse_version("0.40.0") == (0, 40, 0)
    assert laya_decisions._parse_version("1.2") == (1, 2, 0)
    assert laya_decisions._parse_version("garbage") is None
    assert laya_decisions._parse_version("") is None


def test_availability_flags_old_ollama(monkeypatch):
    async def fake_status(base_url=None):
        return {"reachable": True, "version": "0.33.2", "models": [{"name": "laya"}], "error": ""}

    monkeypatch.setattr(laya_decisions.ollama_setup, "get_status", fake_status)
    monkeypatch.setattr(laya_decisions, "_maybe_start_ollama_upgrade", lambda: None)
    monkeypatch.setenv("LAYA_AUTO_UPDATE_OLLAMA", "1")
    # Explicit ollama preference reports the version problem directly.
    monkeypatch.setenv("LAYA_PROVIDER", "ollama")
    result = asyncio.run(laya_decisions.availability(force=True))
    assert result["available"] is False
    assert result["reason"] == "upgrading_ollama"
    assert "Upgrading Ollama automatically in the background" in result["detail"]
    assert "0.40" in result["detail"]


def test_availability_old_ollama_without_auto_upgrade(monkeypatch):
    async def fake_status(base_url=None):
        return {"reachable": True, "version": "0.33.2", "models": [{"name": "laya"}], "error": ""}

    monkeypatch.setattr(laya_decisions.ollama_setup, "get_status", fake_status)
    monkeypatch.setattr(laya_decisions, "_maybe_start_ollama_upgrade", lambda: None)
    monkeypatch.setenv("LAYA_AUTO_UPDATE_OLLAMA", "0")
    monkeypatch.setenv("LAYA_PROVIDER", "ollama")
    result = asyncio.run(laya_decisions.availability(force=True))
    assert result["available"] is False
    assert result["reason"] == "ollama_too_old"


def test_maybe_start_ollama_upgrade_once(monkeypatch):
    started = []

    async def fake_worker():
        started.append("run")

    monkeypatch.setattr(laya_decisions, "_ollama_upgrade_worker", fake_worker)
    monkeypatch.setattr(laya_decisions, "_ollama_upgrade_started", False)
    monkeypatch.setattr(laya_decisions, "_ollama_upgrade_task", None)
    monkeypatch.setenv("LAYA_AUTO_UPDATE_OLLAMA", "1")

    # Must run inside an event loop to create the task.
    async def run():
        laya_decisions._maybe_start_ollama_upgrade()
        await asyncio.sleep(0)
        laya_decisions._maybe_start_ollama_upgrade()  # deduped
        await asyncio.sleep(0)
        return started

    assert asyncio.run(run()) == ["run"]


def test_maybe_start_ollama_upgrade_respects_flag(monkeypatch):
    async def run():
        laya_decisions._maybe_start_ollama_upgrade()
        await asyncio.sleep(0)

    monkeypatch.setattr(laya_decisions, "_ollama_upgrade_started", False)
    monkeypatch.setattr(laya_decisions, "_ollama_upgrade_task", None)
    monkeypatch.setenv("LAYA_AUTO_UPDATE_OLLAMA", "0")
    before = laya_decisions._ollama_upgrade_started
    asyncio.run(run())
    assert laya_decisions._ollama_upgrade_started == before


def test_self_test_passes_offline():
    result = laya_decisions.self_test()
    assert result["ok"] is True
    assert result["passed"] == result["total"]
    assert result["failures"] == []


def test_operation_state_tracks_progress():
    laya_decisions.set_operation_progress("pull", percent=41.5, message="Downloading laya: 41%")
    state = laya_decisions.operation_state()
    assert state["active"] is True
    assert state["operation"] == "pull"
    assert state["percent"] == 41.5
    assert state["message"] == "Downloading laya: 41%"

    laya_decisions.set_operation_progress("pull", percent=100, message="done", done=True)
    state = laya_decisions.operation_state()
    assert state["active"] is False  # done operations stop showing progress

    laya_decisions.set_operation_progress("install_local", message="installing...")
    state = laya_decisions.operation_state()
    assert state["active"] is True
    assert state["percent"] is None  # indeterminate work has no percent
    laya_decisions.set_operation_progress(None, done=True)


def test_operation_state_stale_after_timeout():
    laya_decisions.set_operation_progress("upgrade_ollama", message="working")
    # Simulate the updater dying without a final event: stale after 30 minutes.
    laya_decisions._op_state["updated_at"] = laya_decisions._op_state["updated_at"] - 1900
    state = laya_decisions.operation_state()
    assert state["active"] is False


def test_availability_auto_falls_back_to_local(monkeypatch):
    """With an old Ollama, auto uses the built-in model — 'works by itself'."""
    async def fake_status(base_url=None):
        return {"reachable": True, "version": "0.32.14", "models": [{"name": "laya"}], "error": ""}

    monkeypatch.setattr(laya_decisions.ollama_setup, "get_status", fake_status)
    monkeypatch.setattr(laya_decisions, "local_status", lambda: {
        "available": True, "reason": "ok", "detail": "Built-in laya model ready.",
    })
    monkeypatch.setenv("LAYA_PROVIDER", "auto")
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    monkeypatch.delenv("LAYA_BASE_URL", raising=False)

    result = asyncio.run(laya_decisions.availability(force=True))
    assert result["available"] is True
    assert result["provider"] == "local"
    assert result["reason"] == "local_fallback"
    assert asyncio.run(laya_decisions.effective_provider()) == "local"


def test_availability_auto_reports_local_blockers(monkeypatch):
    """When the built-in model cannot run either, the reason says why.

    With Ollama auto-upgrade on, the active fix (upgrading) takes priority in
    the status; with it off, the local blocker itself is reported.
    """
    async def fake_status(base_url=None):
        return {"reachable": True, "version": "0.32.14", "models": [], "error": ""}

    monkeypatch.setattr(laya_decisions.ollama_setup, "get_status", fake_status)
    monkeypatch.setattr(laya_decisions, "local_status", lambda: {
        "available": False, "reason": "no_disk_space", "detail": "needs 1.5 GB free disk space.",
    })
    monkeypatch.setattr(laya_decisions, "_maybe_start_ollama_upgrade", lambda: None)
    monkeypatch.setattr(laya_decisions, "_maybe_start_local_install", lambda: None)
    monkeypatch.setenv("LAYA_PROVIDER", "auto")
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    monkeypatch.delenv("LAYA_BASE_URL", raising=False)

    monkeypatch.setenv("LAYA_AUTO_UPDATE_OLLAMA", "1")
    result = asyncio.run(laya_decisions.availability(force=True))
    assert result["available"] is False
    assert result["provider"] == "local"
    assert result["reason"] == "upgrading_ollama"
    assert "free disk space" in result["detail"]

    monkeypatch.setenv("LAYA_AUTO_UPDATE_OLLAMA", "0")
    laya_decisions.invalidate_availability()
    result = asyncio.run(laya_decisions.availability(force=True))
    assert result["available"] is False
    assert result["reason"] == "no_disk_space"
    assert "Ollama" in result["detail"]


def test_availability_ok_with_new_ollama(monkeypatch):
    async def fake_status(base_url=None):
        return {"reachable": True, "version": "0.42.1", "models": [{"name": "laya"}], "error": ""}

    monkeypatch.setattr(laya_decisions.ollama_setup, "get_status", fake_status)
    result = asyncio.run(laya_decisions.availability(force=True))
    assert result["available"] is True
    assert result["provider"] == "ollama"


def test_availability_model_missing_without_auto_pull(monkeypatch):
    async def fake_status(base_url=None):
        return {"reachable": True, "version": "0.42.1", "models": [], "error": ""}

    monkeypatch.setattr(laya_decisions.ollama_setup, "get_status", fake_status)
    monkeypatch.setenv("LAYA_PROVIDER", "ollama")
    monkeypatch.setenv("LAYA_AUTO_PULL", "0")
    result = asyncio.run(laya_decisions.availability(force=True))
    assert result["available"] is False
    assert result["reason"] == "model_missing"


def test_availability_disabled(monkeypatch):
    monkeypatch.setenv("LAYA_ENABLED", "0")
    result = asyncio.run(laya_decisions.availability(force=True))
    assert result["available"] is False
    assert result["reason"] == "disabled"


def test_feature_gating(monkeypatch):
    monkeypatch.setenv("LAYA_ENABLED", "1")
    monkeypatch.setenv("LAYA_EMAIL_TRIAGE", "0")
    assert laya_decisions.feature_enabled("email_triage") is False
    assert laya_decisions.feature_enabled("memory_rerank") is True
    assert laya_decisions.feature_enabled("unknown_feature") is False

    monkeypatch.setenv("LAYA_ENABLED", "0")
    assert laya_decisions.feature_enabled("memory_rerank") is False


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------


def test_confidence_min_clamps_and_parses(monkeypatch):
    monkeypatch.setenv("LAYA_CONFIDENCE_MIN", "0.9")
    assert laya_decisions.confidence_min() == 0.9
    monkeypatch.setenv("LAYA_CONFIDENCE_MIN", "9")
    assert laya_decisions.confidence_min() == 1.0
    monkeypatch.setenv("LAYA_CONFIDENCE_MIN", "not a number")
    assert laya_decisions.confidence_min() == 0.75


def test_test_decision_reports_failure(monkeypatch):
    _pin_custom_provider(monkeypatch)
    _patch_http(monkeypatch, exc=httpx.ConnectError("down"))
    result = asyncio.run(laya_decisions.test_decision())
    assert result["ok"] is False
    assert "error" in result


# ---------------------------------------------------------------------------
# Local (built-in) backend
# ---------------------------------------------------------------------------


def test_local_decide_runs_in_process_router(monkeypatch):
    """The local backend passes our wire format straight to the package."""

    class _FakeRouter:
        def predict(self, state, questions, model=None):
            assert questions["dept"]["type"] == "choice"
            assert questions["dept"]["criteria"] == {"tech": "bugs", "other": "rest"}
            assert state == "server is on fire"
            return {
                "answers": {"dept": {"choice": "tech", "confidence": 0.9}},
                "routing": {"model": model or "english"},
            }

    async def fake_get_router():
        return _FakeRouter()

    monkeypatch.setattr(laya_decisions, "_get_local_router", fake_get_router)
    monkeypatch.setattr(laya_decisions, "local_status", lambda: {"available": True, "reason": "ok", "detail": "ready"})

    result = asyncio.run(
        laya_decisions._local_decide(
            "server is on fire",
            {"dept": laya_decisions.choice("?", {"tech": "bugs", "other": "rest"})},
        )
    )
    assert result["provider"] == "local"
    assert result["answers"]["dept"]["choice"] == "tech"
    assert result["latency_ms"] >= 0


def test_local_decide_raises_without_disk_space(monkeypatch):
    monkeypatch.setattr(laya_decisions, "local_status", lambda: {
        "available": False, "reason": "no_disk_space", "detail": "needs 1.5 GB free disk space.",
    })
    try:
        asyncio.run(laya_decisions._local_decide("x", {"q": laya_decisions.noul("?")}))
        raised = False
    except laya_decisions.LayaUnavailableError as exc:
        raised = True
        assert "disk" in str(exc)
    assert raised


def test_local_status_disk_check(monkeypatch):
    monkeypatch.setattr(laya_decisions, "local_package_available", lambda: True)
    monkeypatch.setattr(laya_decisions, "_free_disk_bytes", lambda: 500_000_000)
    status = laya_decisions.local_status()
    assert status["available"] is False
    assert status["reason"] == "no_disk_space"

    monkeypatch.setattr(laya_decisions, "_free_disk_bytes", lambda: 20_000_000_000)
    monkeypatch.setattr(laya_decisions, "local_weights_downloaded", lambda: True)
    status = laya_decisions.local_status()
    assert status["available"] is True
    assert status["reason"] == "ok"


def test_decide_routes_local_when_pinned(monkeypatch):
    """LAYA_PROVIDER=local goes to the in-process backend, not HTTP."""
    called = {"local": False}

    async def fake_local(state, questions):
        called["local"] = True
        return {"answers": {"q": {"noul": 1.0}}, "provider": "local", "model": "english", "latency_ms": 1.0, "raw": {}}

    monkeypatch.setenv("LAYA_PROVIDER", "local")
    monkeypatch.setattr(laya_decisions, "_local_decide", fake_local)

    result = asyncio.run(laya_decisions.decide("x", {"q": laya_decisions.noul("?")}))
    assert called["local"] is True
    assert result["provider"] == "local"


# ---------------------------------------------------------------------------
# AI.py integrations
# ---------------------------------------------------------------------------


def test_laya_decide_tool_registered():
    tools = AI._get_all_tools(include_subagent=True, include_telegram=False)
    names = {t["function"]["name"] for t in tools}
    assert "laya_decide" in names
    assert "browser_auto" in names


def test_browser_tools_category_includes_auto():
    assert "browser_auto" in AI._TOOL_CATEGORIES["browser"][1]


def test_rerank_memories_reorders_and_filters(monkeypatch):
    monkeypatch.setattr(laya_decisions, "feature_enabled", lambda f: True)
    monkeypatch.setattr(
        AI.memory,
        "get_relevant_memories",
        lambda user_id, message, threshold=0.15, limit=5: [
            {"id": 1, "content": "user likes tea", "importance": 5},
            {"id": 2, "content": "user has a dog", "importance": 5},
            {"id": 3, "content": "user works at acme", "importance": 5},
        ],
    )

    async def fake_try_decide(state, questions, feature=None):
        return {
            "answers": {
                "relevant_1": {"noul": 0.95},
                "relevant_2": {"noul": 0.05},
                "relevant_3": {"noul": 0.60},
            }
        }

    async def fake_available():
        return True

    monkeypatch.setattr(laya_decisions, "try_decide", fake_try_decide)
    monkeypatch.setattr(laya_decisions, "available", fake_available)

    ranked = asyncio.run(AI._rerank_memories(1, "what should I drink?"))
    assert ranked is not None
    contents = [m["content"] for m in ranked]
    assert contents == ["user likes tea", "user works at acme"]  # 0.05 filtered below 0.5


def test_rerank_memories_falls_open_when_unavailable(monkeypatch):
    monkeypatch.setattr(laya_decisions, "feature_enabled", lambda f: True)

    async def fake_try_decide(state, questions, feature=None):
        return None

    monkeypatch.setattr(laya_decisions, "try_decide", fake_try_decide)

    async def fake_available():
        return False

    monkeypatch.setattr(laya_decisions, "available", fake_available)

    ranked = asyncio.run(AI._rerank_memories(1, "hello"))
    assert ranked is None


def test_build_system_accepts_prefetched_memories(monkeypatch):
    monkeypatch.setattr(AI.memory, "get_relevant_memories", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")))
    system = AI._build_system(1, "hi", relevant_memories=[{"content": "likes tea", "category": None, "relevance": 0.9, "importance": 5}])
    assert "likes tea" in system
    assert "should not be called" not in system


def test_triage_model_routes_simple_messages(monkeypatch):
    monkeypatch.setenv("FAST_MODEL", "gemini/gemini-2.5-flash")
    monkeypatch.setattr(laya_decisions, "feature_enabled", lambda f: True)

    async def fake_available():
        return True

    async def fake_try_decide(state, questions, feature=None):
        return {
            "answers": {
                "complexity": {"choice": "simple", "confidence": 0.95},
                "needs_tools": {"noul": 0.05},
            }
        }

    monkeypatch.setattr(laya_decisions, "available", fake_available)
    monkeypatch.setattr(laya_decisions, "try_decide", fake_try_decide)
    monkeypatch.setattr(laya_decisions, "confidence_min", lambda: 0.75)
    monkeypatch.setattr(AI.model_router, "resolve_model", lambda m: ("gemini", m))
    monkeypatch.setattr(AI.model_router, "_provider_enabled", lambda p: True)

    picked = asyncio.run(AI._triage_model("anthropic/claude-sonnet-4", "hey what's up"))
    assert picked == "gemini/gemini-2.5-flash"


def test_triage_model_keeps_base_when_tools_needed(monkeypatch):
    monkeypatch.setenv("FAST_MODEL", "gemini/gemini-2.5-flash")
    monkeypatch.setattr(laya_decisions, "feature_enabled", lambda f: True)

    async def fake_available():
        return True

    async def fake_try_decide(state, questions, feature=None):
        return {
            "answers": {
                "complexity": {"choice": "simple", "confidence": 0.95},
                "needs_tools": {"noul": 0.9},
            }
        }

    monkeypatch.setattr(laya_decisions, "available", fake_available)
    monkeypatch.setattr(laya_decisions, "try_decide", fake_try_decide)
    monkeypatch.setattr(AI.model_router, "resolve_model", lambda m: ("gemini", m))
    monkeypatch.setattr(AI.model_router, "_provider_enabled", lambda p: True)

    picked = asyncio.run(AI._triage_model("anthropic/claude-sonnet-4", "check the server logs"))
    assert picked is None


def test_triage_model_needs_fast_model(monkeypatch):
    monkeypatch.delenv("FAST_MODEL", raising=False)
    monkeypatch.setattr(laya_decisions, "feature_enabled", lambda f: True)
    picked = asyncio.run(AI._triage_model("anthropic/claude-sonnet-4", "hey"))
    assert picked is None


# ---------------------------------------------------------------------------
# Email triage digest
# ---------------------------------------------------------------------------


def test_triage_digest_orders_and_formats():
    entries = [
        {"id": "1", "from": "a@x.com", "subject": "newsletter", "category": "newsletter", "urgency": 1.0, "confidence": 0.9},
        {"id": "2", "from": "b@x.com", "subject": "server down", "category": "urgent", "needs_reply_today": 0.9, "urgency": 3.0, "confidence": 0.95},
        {"id": "3", "from": "c@x.com", "subject": "????", "category": None},
    ]
    import email_tools

    digest = email_tools._triage_digest(entries)
    lines = digest.split("\n")
    assert lines[0].startswith("Inbox triage: 3 messages")
    assert "1 urgent" in lines[0]
    # urgent section comes before the newsletter section
    assert digest.index("URGENT") < digest.index("NEWSLETTER")
    assert "UNCLASSIFIED (1)" in digest
    assert "[2] b@x.com" in digest


def test_triage_type_texts_extraction():
    import browser_automation

    texts = browser_automation._auto_pilot_type_texts('search for "best laptops" and open the first result', None)
    assert texts == ["best laptops"]
    texts = browser_automation._auto_pilot_type_texts("no quotes here", ["a", "b"])
    assert texts == ["a", "b"]
    texts = browser_automation._auto_pilot_type_texts('find "laptops" then "monitors"', None)
    assert texts == ["laptops", "monitors"]
