import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from dotenv import load_dotenv
from google import genai
import app_paths

load_dotenv(dotenv_path=app_paths.env_file_path())

logger = logging.getLogger("talos.websearch")

_client: genai.Client | None = None
_search_model: str | None = None
_quota_retry_after: float = 0.0

_CANDIDATE_MODELS = [
    "gemini-3.1-flash-lite",
    "gemini-3.1-flash-lite-preview",
    "gemini-3-flash-preview",
    "gemini-3.8-flash",
    "gemini-3.5-flash",
    "gemini-flash-latest",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
]

_PROBE_BACKOFF_S = 1800  # retry the probe at most every 30 minutes after a failure


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set in .env")
        _client = genai.Client(api_key=api_key)
    return _client


def reload_client():
    global _client, _search_model, _quota_retry_after
    _client = None
    _search_model = None
    _quota_retry_after = 0.0
    load_dotenv(dotenv_path=app_paths.env_file_path(), override=True)


def _probe_model(client: genai.Client, name: str) -> bool:
    try:
        response = client.models.generate_content(
            model=name,
            contents="test",
            config={"tools": [{"google_search": {}}]},
        )
        return bool(response and getattr(response, "candidates", None))
    except Exception:
        return False


def _find_search_model() -> str | None:
    global _search_model, _quota_retry_after
    if _search_model is not None:
        return _search_model

    # Backoff: don't hammer ~20 candidate endpoints when quota is exhausted.
    if time.time() < _quota_retry_after:
        return None

    client = _get_client()

    for model_name in _CANDIDATE_MODELS:
        if _probe_model(client, model_name):
            _search_model = model_name
            logger.info(f"Web search model selected: {model_name}")
            return _search_model

    # Auto-discovery fallback for models not in the candidate list.
    try:
        for m in client.models.list():
            name = getattr(m, "name", "").removeprefix("models/")
            if "flash" in name.lower() and name not in _CANDIDATE_MODELS:
                if _probe_model(client, name):
                    _search_model = name
                    logger.info(f"Web search model auto-discovered: {name}")
                    return _search_model
    except Exception:
        pass

    _quota_retry_after = time.time() + _PROBE_BACKOFF_S
    logger.warning(
        f"No Gemini model with google_search support found; next probe in {_PROBE_BACKOFF_S}s"
    )
    return None


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
    """DuckDuckGo HTML search fallback used when Gemini grounding is unavailable."""
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


def web_search(query: str, scope: str = "", location: str = "", recent_days: int = 0) -> dict:
    if not query or not query.strip():
        return {"error": "No search query provided"}

    model = _find_search_model()
    if not model:
        logger.warning("Gemini search model unavailable, using DuckDuckGo fallback")
        return _ddg_fallback(query, scope, location, recent_days)

    try:
        client = _get_client()

        search_prompt = f"Search the web for: {query.strip()}"
        if scope and scope.strip():
            search_prompt += f" (focus: {scope.strip()})"
        if location and location.strip():
            search_prompt += f" (location: {location.strip()})"
        if recent_days and recent_days > 0:
            search_prompt += f" (from the last {recent_days} days)"

        search_prompt += "\n\nProvide a summary of the most relevant search results with titles, URLs, and key information."

        response = client.models.generate_content(
            model=model,
            contents=search_prompt,
            config={
                "tools": [{"google_search": {}}],
            },
        )

        results = []

        if hasattr(response, "candidates") and response.candidates:
            candidate = response.candidates[0]
            if hasattr(candidate, "content") and candidate.content:
                if hasattr(candidate.content, "parts") and candidate.content.parts:
                    text_content = ""
                    for part in candidate.content.parts:
                        if hasattr(part, "text"):
                            text_content += part.text or ""

                    if text_content:
                        results.append({
                            "title": "Search Results Summary",
                            "link": "",
                            "content": text_content,
                            "media": "Google Search via Gemini",
                        })

        grounding_chunks = []
        if hasattr(response, "candidates") and response.candidates:
            candidate = response.candidates[0]
            if hasattr(candidate, "grounding_metadata") and candidate.grounding_metadata:
                gm = candidate.grounding_metadata
                if hasattr(gm, "grounding_chunks") and gm.grounding_chunks:
                    for chunk in gm.grounding_chunks:
                        if hasattr(chunk, "web") and chunk.web:
                            grounding_chunks.append({
                                "title": getattr(chunk.web, "title", ""),
                                "link": getattr(chunk.web, "uri", ""),
                            })

        if grounding_chunks:
            results = []
            for chunk in grounding_chunks:
                results.append({
                    "title": chunk.get("title", ""),
                    "link": chunk.get("link", ""),
                    "content": "",
                    "media": "Google Search",
                })

        return {
            "query": query,
            "results": results,
            "result_count": len(results),
            "source": "Google Search via Gemini",
        }

    except Exception as e:
        logger.warning(f"Gemini web search failed ({str(e)[:120]}), falling back to DuckDuckGo")
        return _ddg_fallback(query, scope, location, recent_days)
