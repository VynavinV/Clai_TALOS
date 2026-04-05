import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import time
from typing import Any
from urllib.parse import urljoin, urldefrag

import httpx
from dotenv import load_dotenv

try:
    from scrapy import Selector

    SCRAPY_AVAILABLE = True
except Exception:
    Selector = None
    SCRAPY_AVAILABLE = False

load_dotenv()

logger = logging.getLogger("talos.scrapy_scraper")

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_CACHE_DIR = os.path.join(_SCRIPT_DIR, "logs", "scrape_cache")
_CACHE_INDEX_PATH = os.path.join(_CACHE_DIR, "index.json")


_ALLOWED_SCHEMES = {"http", "https"}


def _is_private_ip(hostname: str) -> bool:
    try:
        addrs = socket.getaddrinfo(hostname, None)
        for family, _, _, _, sockaddr in addrs:
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return True
    except (socket.gaierror, ValueError):
        pass
    return False


def _validate_url(url: str) -> str | None:
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return f"URL scheme must be http or https, got '{parsed.scheme}'"
    hostname = parsed.hostname or ""
    if not hostname:
        return "URL must include a hostname"
    if hostname.endswith(".local") or hostname.endswith(".internal"):
        return "Access to local/internal hostnames is blocked"
    if _is_private_ip(hostname):
        return "Access to private/internal IP addresses is blocked"
    return None

_SUPPORTED_FORMATS = {"markdown", "html", "links", "screenshot"}
_TEXT_BLOCK_XPATH = ".//h1|.//h2|.//h3|.//h4|.//h5|.//h6|.//p|.//li|.//blockquote|.//pre"
_WS_RE = re.compile(r"\s+")


def reload_client():
    load_dotenv(override=True)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _normalize_formats(formats: list[str] | None) -> list[str]:
    if not formats:
        return ["markdown"]
    normalized: list[str] = []
    for fmt in formats:
        value = str(fmt).strip().lower()
        if value in _SUPPORTED_FORMATS and value not in normalized:
            normalized.append(value)
    return normalized or ["markdown"]


def _clean_text(text: str) -> str:
    return _WS_RE.sub(" ", str(text or "")).strip()


def _ensure_cache_dir() -> None:
    os.makedirs(_CACHE_DIR, exist_ok=True)


def _load_cache_index() -> dict[str, dict]:
    _ensure_cache_dir()
    if not os.path.isfile(_CACHE_INDEX_PATH):
        return {}
    try:
        with open(_CACHE_INDEX_PATH, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cache_index(index: dict[str, dict]) -> None:
    _ensure_cache_dir()
    tmp = _CACHE_INDEX_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(index, f, indent=2)
    os.replace(tmp, _CACHE_INDEX_PATH)


def _make_cache_key(url: str, formats: list[str], only_main_content: bool) -> str:
    payload = {
        "url": url,
        "formats": formats,
        "only_main_content": bool(only_main_content),
    }
    raw = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _read_cached_result(cache_key: str, max_age_ms: int) -> dict | None:
    if max_age_ms <= 0:
        return None
    index = _load_cache_index()
    entry = index.get(cache_key)
    if not isinstance(entry, dict):
        return None
    saved_ms = int(entry.get("saved_ms", 0) or 0)
    if saved_ms <= 0 or (_now_ms() - saved_ms) > max_age_ms:
        return None
    path = str(entry.get("path", ""))
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data["cached"] = True
            return data
    except Exception:
        return None
    return None


def _write_cached_result(cache_key: str, result: dict) -> None:
    _ensure_cache_dir()
    path = os.path.join(_CACHE_DIR, f"{cache_key}.json")
    with open(path, "w") as f:
        json.dump(result, f, ensure_ascii=False)
    index = _load_cache_index()
    index[cache_key] = {
        "saved_ms": _now_ms(),
        "path": path,
    }
    _save_cache_index(index)


def _pick_main_node(selector: Any) -> Any:
    candidate_css = [
        "article",
        "main",
        "#content",
        "[role='main']",
        ".content",
        ".post",
        ".article",
        ".main-content",
    ]
    best = None
    best_len = 0

    for css in candidate_css:
        for node in selector.css(css):
            text = _clean_text(" ".join(node.xpath(".//text()").getall()))
            if len(text) > best_len:
                best = node
                best_len = len(text)
        if best_len >= 400:
            break

    if best is not None:
        return best

    body_nodes = selector.xpath("//body")
    if body_nodes:
        return body_nodes[0]
    return selector


def _extract_links(selector: Any, base_url: str) -> list[str]:
    seen = set()
    links: list[str] = []
    for href in selector.css("a::attr(href)").getall():
        raw = str(href or "").strip()
        if not raw:
            continue
        absolute = urljoin(base_url, raw)
        absolute, _ = urldefrag(absolute)
        if not absolute or absolute in seen:
            continue
        seen.add(absolute)
        links.append(absolute)
    return links


def _node_to_markdown(node: Any) -> str:
    tag = str(getattr(getattr(node, "root", None), "tag", "")).lower()

    if tag == "pre":
        code = "\n".join([line.rstrip() for line in node.xpath(".//text()").getall()]).strip()
        if not code:
            return ""
        return f"```\n{code}\n```"

    text = _clean_text(" ".join(node.xpath(".//text()").getall()))
    if not text:
        return ""

    if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        level = int(tag[1]) if len(tag) == 2 and tag[1].isdigit() else 2
        level = max(1, min(level, 6))
        return ("#" * level) + " " + text
    if tag == "li":
        return "- " + text
    if tag == "blockquote":
        return "> " + text
    return text


def _extract_markdown(selector: Any) -> str:
    parts: list[str] = []
    for block in selector.xpath(_TEXT_BLOCK_XPATH):
        line = _node_to_markdown(block)
        if line:
            parts.append(line)

    if parts:
        return "\n\n".join(parts)

    fallback = _clean_text(" ".join(selector.xpath(".//text()").getall()))
    return fallback


def scrape_url(
    url: str,
    formats: list[str] | None = None,
    only_main_content: bool = True,
    timeout: int = 30000,
    max_age: int = 172800000,
) -> dict:
    raw_url = str(url or "").strip()
    if not raw_url:
        return {"error": "No URL provided"}

    url_error = _validate_url(raw_url)
    if url_error:
        return {"error": url_error, "url": raw_url}

    if not SCRAPY_AVAILABLE:
        return {
            "error": "Scrapy is not installed. Install dependencies (pip install -r requirements.txt).",
            "url": raw_url,
        }

    selected_formats = _normalize_formats(formats)
    timeout_ms = min(max(1000, int(timeout or 30000)), 300000)
    max_age_ms = max(0, int(max_age or 0))

    cache_key = _make_cache_key(raw_url, selected_formats, bool(only_main_content))
    cached = _read_cached_result(cache_key, max_age_ms)
    if cached is not None:
        return cached

    try:
        timeout_s = max(1.0, min(timeout_ms / 1000.0, 120.0))
        with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
            response = client.get(
                raw_url,
                headers={
                    "User-Agent": "Clai-TALOS/1.0 (+local-scrapy)",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )

        if response.status_code >= 400:
            return {
                "error": f"Failed to fetch URL (HTTP {response.status_code})",
                "url": raw_url,
                "status_code": response.status_code,
            }

        final_url = str(response.url)
        html = response.text
        selector = Selector(text=html)
        main_node = _pick_main_node(selector) if only_main_content else selector

        result: dict = {
            "url": raw_url,
            "success": True,
            "source": "local-scrapy",
            "cached": False,
        }

        warnings: list[str] = []

        if "markdown" in selected_formats:
            result["markdown"] = _extract_markdown(main_node)

        if "html" in selected_formats:
            result["html"] = main_node.get() if hasattr(main_node, "get") else html

        if "links" in selected_formats:
            link_scope = main_node if only_main_content else selector
            result["links"] = _extract_links(link_scope, final_url)

        if "screenshot" in selected_formats:
            warnings.append("screenshot is not supported by local Scrapy scraping; use browser screenshot tools")

        if warnings:
            result["warnings"] = warnings

        title = _clean_text(selector.css("title::text").get() or "")
        description = _clean_text(selector.css("meta[name='description']::attr(content)").get() or "")
        language = _clean_text(selector.xpath("/html/@lang").get() or "")
        result["metadata"] = {
            "title": title,
            "description": description,
            "language": language,
            "final_url": final_url,
        }

        _write_cached_result(cache_key, result)
        return result

    except httpx.TimeoutException:
        return {"error": "Scraping timed out", "url": raw_url}
    except Exception as e:
        logger.exception(f"Local scrape error: {e}")
        return {"error": str(e), "url": raw_url}
