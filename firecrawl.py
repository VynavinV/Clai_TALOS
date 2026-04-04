import os
import logging
import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("talos.firecrawl")

FIRECRAWL_API_URL = "https://api.firecrawl.dev/v2/scrape"
_api_key: str | None = None


def _get_api_key() -> str:
    global _api_key
    if _api_key is None:
        _api_key = os.getenv("FIRECRAWL_API_KEY", "")
    return _api_key


def reload_client():
    global _api_key
    _api_key = None
    load_dotenv(override=True)


def scrape_url(
    url: str,
    formats: list[str] | None = None,
    only_main_content: bool = True,
    timeout: int = 30000,
    max_age: int = 172800000,
) -> dict:
    if not url or not url.strip():
        return {"error": "No URL provided"}
    
    api_key = _get_api_key()
    if not api_key:
        return {"error": "FIRECRAWL_API_KEY not set in .env"}
    
    if formats is None:
        formats = ["markdown"]
    
    payload = {
        "url": url.strip(),
        "formats": [{"type": fmt} for fmt in formats],
        "onlyMainContent": only_main_content,
        "timeout": min(max(1000, timeout), 300000),
        "maxAge": max_age,
    }
    
    try:
        with httpx.Client(timeout=(timeout / 1000) + 10) as client:
            response = client.post(
                FIRECRAWL_API_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            
            if response.status_code == 402:
                return {"error": "Payment required - Firecrawl credits exhausted", "url": url}
            elif response.status_code == 429:
                return {"error": "Rate limit exceeded - try again later", "url": url}
            elif response.status_code >= 400:
                return {"error": f"Firecrawl API error: {response.status_code}", "url": url}
            
            data = response.json()
            
            if not data.get("success"):
                return {"error": "Scraping failed", "url": url}
            
            result = {
                "url": url,
                "success": True,
            }
            
            scrape_data = data.get("data", {})
            
            if "markdown" in formats and scrape_data.get("markdown"):
                result["markdown"] = scrape_data["markdown"]
            
            if "html" in formats and scrape_data.get("html"):
                result["html"] = scrape_data["html"]
            
            if "links" in formats and scrape_data.get("links"):
                result["links"] = scrape_data["links"]
            
            if "screenshot" in formats and scrape_data.get("screenshot"):
                result["screenshot"] = scrape_data["screenshot"]
            
            metadata = scrape_data.get("metadata", {})
            if metadata:
                result["metadata"] = {
                    "title": metadata.get("title"),
                    "description": metadata.get("description"),
                    "language": metadata.get("language"),
                    "final_url": metadata.get("url"),
                }
            
            return result
            
    except httpx.TimeoutException:
        return {"error": "Scraping timed out", "url": url}
    except Exception as e:
        logger.exception(f"Firecrawl error: {e}")
        return {"error": str(e), "url": url}
