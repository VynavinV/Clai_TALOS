import os
import logging
from typing import Any
from dotenv import load_dotenv
from google import genai

load_dotenv()

logger = logging.getLogger("talos.websearch")

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set in .env")
        _client = genai.Client(api_key=api_key)
    return _client


def reload_client():
    global _client
    _client = None
    load_dotenv(override=True)


def web_search(query: str, scope: str = "", location: str = "", recent_days: int = 0) -> dict[str, Any]:
    if not query or not query.strip():
        return {"error": "No search query provided"}
    
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
            model="gemini-2.0-flash-exp",
            contents=search_prompt,
            config={
                "tools": [{"google_search_retrieval": {}}],
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
        logger.exception(f"Web search failed: {e}")
        return {"error": str(e), "query": query}
