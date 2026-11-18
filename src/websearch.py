"""Web search via Parallel's zero-auth MCP service, with a DuckDuckGo fallback.

No API key is needed: the endpoint at https://mcp.parallel.ai/mcp speaks the
Model Context Protocol over Streamable HTTP and serves hobbyist rate limits
for free. If it is unreachable, search degrades to a DuckDuckGo HTML scrape.
"""

import json
import logging
import re
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dotenv import load_dotenv
import app_paths

load_dotenv(dotenv_path=app_paths.env_file_path())

logger = logging.getLogger("talos.websearch")

_MCP_URL = os.getenv("PARALLEL_MCP_URL", "https://mcp.parallel.ai/mcp").rstrip("/")
_MCP_PROTOCOL_VERSION = "2025-06-18"
_MCP_TIMEOUT_S = 45
_INIT_BACKOFF_S = 300  # after a failed handshake, wait before hammering the endpoint

_lock = threading.Lock()
_session_id: str | None = None
_rpc_id = 0
_init_backoff_until = 0.0


# ---------------------------------------------------------------------------
# Minimal MCP client (JSON-RPC 2.0 over Streamable HTTP, stdlib only)
# ---------------------------------------------------------------------------

def _decode_body(body: str) -> dict | None:
    """Parse one JSON-RPC message from an application/json or SSE response."""
    body = body.strip()
    if not body:
        return None
    if body.startswith("{"):
        return json.loads(body)
    # SSE: one "data: {...}" line per message; HTTPError-free servers may also
    # send "event:" lines, which carry no payload.
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[len("data:"):].strip()
            if payload:
                msg = json.loads(payload)
                if isinstance(msg, dict) and msg.get("id") is not None:
                    return msg
    return None


def _post(payload: dict, session_id: str | None) -> tuple[str | None, dict | None]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    req = urllib.request.Request(
        _MCP_URL, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=_MCP_TIMEOUT_S) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            new_session = resp.headers.get("Mcp-Session-Id")
    except urllib.error.HTTPError as e:
        # Re-surface the body so protocol errors (e.g. expired session) can be
        # inspected by the caller; drain it so the connection is released.
        detail = e.read().decode("utf-8", errors="replace")[:200] if e.fp else str(e)
        raise RuntimeError(f"MCP request failed with HTTP {e.code}: {detail}") from e
    return new_session, _decode_body(body)


def _ensure_session() -> str | None:
    """Handshake once and cache the server-issued session id."""
    global _session_id, _init_backoff_until
    with _lock:
        if _session_id:
            return _session_id
        if time.time() < _init_backoff_until:
            raise RuntimeError("Parallel search service unreachable (retry backoff active)")
        try:
            session_id, msg = _post(
                {
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": _MCP_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "Clai-TALOS", "version": "1.0.0"},
                    },
                },
                None,
            )
            if msg is not None and "error" in msg:
                raise RuntimeError(f"MCP initialize rejected: {msg['error']}")
            _session_id = session_id or ""
            _post(
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                _session_id or None,
            )
            logger.info("Parallel search session established")
            return _session_id or None
        except Exception as e:
            _init_backoff_until = time.time() + _INIT_BACKOFF_S
            logger.warning(f"Parallel search handshake failed: {str(e)[:160]}")
            raise


def _mcp_call(method: str, params: dict) -> dict:
    """Send a JSON-RPC request; on a dead session, re-handshake and retry once."""
    global _rpc_id, _session_id
    session_id = _ensure_session()
    last_error: Exception | None = None
    for _attempt in (1, 2):
        with _lock:
            _rpc_id += 1
            request_id = _rpc_id
        try:
            _, msg = _post(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                session_id,
            )
        except RuntimeError as e:
            # Most likely an expired session id: drop it and re-handshake once.
            last_error = e
            with _lock:
                _session_id = None
            session_id = _ensure_session()
            continue
        if msg is None:
            raise RuntimeError("Parallel search service returned no response")
        if "error" in msg:
            raise RuntimeError(f"MCP error: {msg['error']}")
        return msg["result"]
    raise last_error or RuntimeError("Parallel search session could not be re-established")


def reload_client():
    """Drop the cached MCP session so the next search re-handshakes."""
    global _session_id, _init_backoff_until
    with _lock:
        _session_id = None
        _init_backoff_until = 0.0


def list_tools() -> list[str]:
    """Tool names advertised by the search service (used by the system check)."""
    result = _mcp_call("tools/list", {})
    return [t.get("name", "") for t in result.get("tools", []) if isinstance(t, dict)]


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def _build_objective(query: str, scope: str, location: str, recent_days: int) -> str:
    objective = f"Find current information about: {query.strip()}"
    if scope:
        objective += f" Focus on {scope.strip()}."
    if location:
        objective += f" Relevant to {location.strip()}."
    if recent_days and recent_days > 0:
        objective += f" Prefer results published within the last {recent_days} days."
    return objective


def _build_queries(query: str, scope: str, location: str) -> list[str]:
    queries = [re.sub(r"\s+", " ", query.strip())]
    if scope:
        queries.append(f"{query.strip()} {scope.strip()}")
    if location:
        queries.append(f"{query.strip()} {location.strip()}")
    return queries


def _parse_search_result(result: dict) -> list[dict]:
    """Normalize a tools/call result into the web_search result shape."""
    raw: dict = {}
    if isinstance(result.get("structuredContent"), dict):
        raw = result["structuredContent"]
    else:
        texts = [
            item.get("text", "")
            for item in result.get("content", [])
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        for text in texts:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    raw = parsed
                    break
            except (ValueError, TypeError):
                pass
        if not raw.get("results") and texts:
            # Server sent prose instead of its usual JSON payload.
            raw = {"results": [{"title": "Search Results Summary", "url": "", "excerpts": texts}]}

    results = []
    for item in raw.get("results", []):
        if not isinstance(item, dict):
            continue
        results.append({
            "title": item.get("title", ""),
            "link": item.get("url", ""),
            "content": "\n\n".join(e for e in item.get("excerpts", []) if e),
            "publish_date": item.get("publish_date", ""),
            "media": "Parallel",
        })
    return results


def web_search(query: str, scope: str = "", location: str = "", recent_days: int = 0) -> dict:
    if not query or not query.strip():
        return {"error": "No search query provided"}

    try:
        result = _mcp_call(
            "tools/call",
            {
                "name": "web_search",
                "arguments": {
                    "objective": _build_objective(query, scope, location, recent_days),
                    "search_queries": _build_queries(query, scope, location),
                },
            },
        )
        if isinstance(result, dict) and result.get("isError"):
            raise RuntimeError("Parallel search reported an error result")
        results = _parse_search_result(result)
        if not results:
            return {"error": "Parallel search returned no results", "query": query}

        payload = {
            "query": query,
            "results": results,
            "result_count": len(results),
            "source": "Parallel Web Search",
        }
        if any(r["content"] for r in results):
            # answer-ready excerpts — tell the model it may skip scraping
            payload["note"] = "Excerpts are usually sufficient; scrape links only if more detail is needed."
        return payload

    except Exception as e:
        logger.warning(f"Parallel web search failed ({str(e)[:160]}), falling back to DuckDuckGo")
        return _ddg_fallback(query, scope, location, recent_days)


# ---------------------------------------------------------------------------
# DuckDuckGo HTML fallback
# ---------------------------------------------------------------------------

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"


def _fetch(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _strip_tags(html: str) -> str:
    html = re.sub(r"<script.*?</script>|<style.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html).strip()


def _ddg_fallback(query: str, scope: str = "", location: str = "", recent_days: int = 0) -> dict:
    """DuckDuckGo HTML search fallback used when the Parallel service is unreachable."""
    try:
        q = query.strip()
        if scope and scope.strip():
            q += f" {scope.strip()}"
        if location and location.strip():
            q += f" {location.strip()}"
        html = _fetch(f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(q)}")
        results = []
        for m in re.finditer(
            r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL
        ):
            link = m.group(1)
            if "uddg=" in link:
                link = urllib.parse.unquote(link.split("uddg=", 1)[1].split("&", 1)[0])
            title = _strip_tags(m.group(2))
            if title and link.startswith("http"):
                results.append({"title": title, "link": link, "content": "", "media": "DuckDuckGo"})
            if len(results) >= 10:
                break
        if not results:
            return {"error": "DuckDuckGo fallback returned no results", "query": query}
        return {
            "query": query,
            "results": results,
            "result_count": len(results),
            "source": "DuckDuckGo (fallback)",
        }
    except Exception as e:
        logger.exception(f"DDG fallback failed: {e}")
        return {"error": f"DuckDuckGo fallback failed: {e}", "query": query}
