import os
import json
import logging
import asyncio
import time
from typing import Any
from contextvars import ContextVar
from urllib.parse import urlparse, urlunparse
from dotenv import load_dotenv
import app_paths

load_dotenv(dotenv_path=app_paths.env_file_path())

logger = logging.getLogger("talos.router")

_PROVIDERS = {
    "openai": {
        "models": {
            "gpt4o": "gpt-4o",
            "gpt4omini": "gpt-4o-mini",
            "gpt41": "gpt-4.1",
            "gpt41mini": "gpt-4.1-mini",
            "gpt41nano": "gpt-4.1-nano",
            "o3": "o3",
            "o4mini": "o4-mini",
        },
        "patterns": ["gpt", "o3", "o4"],
        "env_key": "OPENAI_API_KEY",
    },
    "anthropic": {
        "models": {
            "claude4sonnet": "claude-sonnet-4-20250514",
            "claude35sonnet": "claude-3-5-sonnet-20241022",
            "claude35haiku": "claude-3-5-haiku-20241022",
            "claude3opus": "claude-3-opus-20240229",
        },
        "patterns": ["claude"],
        "env_key": "ANTHROPIC_API_KEY",
    },
    "gemini": {
        "models": {
            "gemini25pro": "gemini-2.5-pro",
            "gemini25flash": "gemini-2.5-flash",
            "gemini25flashlite": "gemini-2.5-flash-lite",
            "gemini20flash": "gemini-2.0-flash",
            "gemini20flashlite": "gemini-2.0-flash-lite",
        },
        "patterns": ["gemini", "flash", "pro"],
        "env_key": "GEMINI_API_KEY",
    },
    "zhipu": {
        "models": {
            "glm4": "glm-4",
            "glm4v": "glm-4v",
            "glm5": "glm-5",
            "glm5turbo": "glm-5-turbo",
            "charglm3": "charglm-3",
        },
        "patterns": ["glm", "charglm"],
        "env_key": "ZHIPUAI_API_KEY",
    },
    "nvidia": {
        "models": {
            "glm47": "z-ai/glm4.7",
            "glm4_7": "z-ai/glm4.7",
            "glm4.7": "z-ai/glm4.7",
        },
        "patterns": ["nvidia"],
        "env_key": "NVIDIA_API_KEY",
    },
    "cerebras": {
        "models": {
            "llama4": "llama4-scout-17b-16e-instruct",
            "llama31": "llama-3.3-70b",
        },
        "patterns": ["cerebras", "llama"],
        "env_key": "CEREBRAS_API_KEY",
    },
    "groq": {
        "models": {
            "llama33": "llama-3.3-70b-versatile",
            "llama31instant": "llama-3.1-8b-instant",
            "gptoss120b": "openai/gpt-oss-120b",
            "gptoss20b": "openai/gpt-oss-20b",
            "kimik2": "moonshotai/kimi-k2-instruct",
        },
        "patterns": ["groq"],
        "env_key": "GROQ_API_KEY",
    },
    "openrouter": {
        "models": {
            "claude4sonnet": "anthropic/claude-sonnet-4-20250514",
            "claude35sonnet": "anthropic/claude-3.5-sonnet-20241022",
            "gpt4o": "openai/gpt-4o",
            "gpt41": "openai/gpt-4.1",
            "gemini25pro": "google/gemini-2.5-pro-preview",
            "llama4": "meta-llama/llama-4-maverick",
            "deepseekr1": "deepseek/deepseek-r1",
            "qwen3": "qwen/qwen3-235b-a22b",
        },
        "patterns": ["anthropic/", "openai/", "google/", "meta-llama/", "deepseek/", "qwen/", "mistralai/"],
        "env_key": "OPENROUTER_API_KEY",
    },
    "mistral": {
        "models": {
            "mistrallarge": "mistral-large-latest",
            "mistralmedium": "mistral-medium-latest",
            "mistralsmall": "mistral-small-latest",
            "ministral8b": "ministral-8b-latest",
            "magistralmedium": "magistral-medium-latest",
            "codestral": "codestral-latest",
            "pixtrallarge": "pixtral-large-latest",
        },
        "patterns": ["mistral", "ministral", "magistral", "codestral", "devstral", "pixtral"],
        "env_key": "MISTRAL_API_KEY",
    },
    "ollama": {
        "models": {},
        "patterns": ["ollama"],
        "env_key": "OLLAMA_MODEL",
    },
}

_openai_client = None
_anthropic_client = None
_gemini_client = None
_zhipu_client = None
_nvidia_client = None
_cerebras_client = None
_groq_client = None
_openrouter_client = None
_mistral_client = None
_ollama_client = None

_NVIDIA_DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
_NVIDIA_OPENAI_API_PATH = "/v1"
_NVIDIA_MODELS_PATH = "/models"


def _normalize_nvidia_base_url(base_url: str | None) -> str:
    value = str(base_url or "").strip().strip('"').strip("'")
    if not value:
        return _NVIDIA_DEFAULT_BASE_URL

    if "://" not in value:
        value = f"https://{value}"

    parsed = urlparse(value)
    host = (parsed.netloc or "").lower()
    if not host:
        return _NVIDIA_DEFAULT_BASE_URL

    # Users sometimes paste docs URLs by mistake; force the real API host.
    if "docs.api.nvidia.com" in host:
        return _NVIDIA_DEFAULT_BASE_URL

    path = (parsed.path or "").strip()
    if path.endswith(_NVIDIA_MODELS_PATH):
        path = path[: -len(_NVIDIA_MODELS_PATH)]
    if path.endswith("/chat/completions"):
        path = path[: -len("/chat/completions")]

    parts = [p for p in path.split("/") if p]
    if "v1" in parts:
        v1_index = parts.index("v1")
        normalized_path = "/" + "/".join(parts[: v1_index + 1])
    else:
        normalized_path = _NVIDIA_OPENAI_API_PATH

    return urlunparse((parsed.scheme or "https", parsed.netloc, normalized_path, "", "", ""))


def _nvidia_endpoint(path: str) -> str:
    base = _normalize_nvidia_base_url(_NVIDIA_BASE_URL).rstrip("/")
    suffix = path if path.startswith("/") else f"/{path}"
    return f"{base}{suffix}"


_NVIDIA_BASE_URL = _normalize_nvidia_base_url(os.getenv("NVIDIA_BASE_URL", _NVIDIA_DEFAULT_BASE_URL))
_CEREBRAS_BASE_URL = os.getenv("CEREBRAS_BASE_URL", "https://api.cerebras.ai/v1")
_GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
_OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
_MISTRAL_BASE_URL = os.getenv("MISTRAL_BASE_URL", "https://api.mistral.ai/v1")
_OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")

_CLIENT_BASE_URL = os.getenv("CLIENT_BASE_URL", "https://api.z.ai/api/coding/paas/v4")

_NVIDIA_MODEL_ALIASES = {
    "z-ai/glm4_7": "z-ai/glm4.7",
    "z-ai/glm4.7": "z-ai/glm4.7",
    "glm4_7": "z-ai/glm4.7",
    "glm4.7": "z-ai/glm4.7",
}

_PROVIDER_HINT_ALIASES = {
    "z-ai": "nvidia",
    "nim": "nvidia",
}


def _normalize_nvidia_model_id(model_id: str) -> str:
    return _NVIDIA_MODEL_ALIASES.get(str(model_id).lower(), model_id)


def resolve_model(model: str) -> tuple[str, str]:
    if "/" in model and not model.startswith("http"):
        provider_hint, model_id = model.split("/", 1)
        provider_hint = provider_hint.lower().strip()
        provider_hint = _PROVIDER_HINT_ALIASES.get(provider_hint, provider_hint)
        if provider_hint == "nvidia":
            model_id = _normalize_nvidia_model_id(model_id)
        for provider_name in _PROVIDERS:
            if provider_name == provider_hint:
                return provider_name, model_id

    model_lower = model.lower().strip()
    if model_lower in _NVIDIA_MODEL_ALIASES:
        return "nvidia", _normalize_nvidia_model_id(model_lower)
    if model_lower.startswith("z-ai/"):
        return "nvidia", _normalize_nvidia_model_id(model_lower)

    # Exact aliases win over patterns: a broad pattern like Cerebras' "llama"
    # would otherwise swallow another provider's exact alias.
    for provider_name, provider_cfg in _PROVIDERS.items():
        if model_lower in provider_cfg["models"]:
            return provider_name, provider_cfg["models"][model_lower]

    for provider_name, provider_cfg in _PROVIDERS.items():
        for pattern in provider_cfg["patterns"]:
            if pattern in model_lower:
                return provider_name, model
    return "zhipu", model


def get_all_model_aliases() -> dict[str, str]:
    aliases = {}
    for provider_cfg in _PROVIDERS.values():
        aliases.update(provider_cfg["models"])
    return aliases


def reload_clients():
    global _openai_client, _anthropic_client, _gemini_client, _zhipu_client, _nvidia_client, _cerebras_client, _groq_client, _openrouter_client, _mistral_client, _ollama_client
    global _CLIENT_BASE_URL, _NVIDIA_BASE_URL, _CEREBRAS_BASE_URL, _GROQ_BASE_URL, _OPENROUTER_BASE_URL, _MISTRAL_BASE_URL, _OLLAMA_BASE_URL
    _openai_client = None
    _anthropic_client = None
    _gemini_client = None
    _zhipu_client = None
    _nvidia_client = None
    _cerebras_client = None
    _groq_client = None
    _openrouter_client = None
    _mistral_client = None
    _ollama_client = None
    load_dotenv(dotenv_path=app_paths.env_file_path(), override=True)
    _CLIENT_BASE_URL = os.getenv("CLIENT_BASE_URL", "https://api.z.ai/api/coding/paas/v4")
    _NVIDIA_BASE_URL = _normalize_nvidia_base_url(os.getenv("NVIDIA_BASE_URL", _NVIDIA_DEFAULT_BASE_URL))
    _CEREBRAS_BASE_URL = os.getenv("CEREBRAS_BASE_URL", "https://api.cerebras.ai/v1")
    _GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
    _OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    _MISTRAL_BASE_URL = os.getenv("MISTRAL_BASE_URL", "https://api.mistral.ai/v1")
    _OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")

    # Config just changed: re-resolve context windows and let the warm-up loop
    # re-check which local model to hold in memory instead of waiting out its cycle.
    import ollama_setup
    ollama_setup.forget_model_info()
    ollama_setup.request_warm_refresh()


def _get_openai_client():
    global _openai_client
    if _openai_client is None:
        from openai import AsyncOpenAI
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set")
        base_url = os.getenv("OPENAI_BASE_URL")
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        _openai_client = AsyncOpenAI(**kwargs)
    return _openai_client


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        base_url = os.getenv("ANTHROPIC_BASE_URL")
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        _anthropic_client = anthropic.AsyncAnthropic(**kwargs)
    return _anthropic_client


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        import google.genai as genai
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set")
        _gemini_client = genai.Client(api_key=api_key)
    return _gemini_client


def _get_zhipu_client():
    global _zhipu_client
    if _zhipu_client is None:
        from zhipuai import ZhipuAI
        api_key = os.getenv("ZHIPUAI_API_KEY")
        if not api_key:
            raise RuntimeError("ZHIPUAI_API_KEY not set")
        _zhipu_client = ZhipuAI(api_key=api_key, base_url=_CLIENT_BASE_URL)
    return _zhipu_client


def _get_nvidia_client():
    global _nvidia_client
    if _nvidia_client is None:
        from openai import AsyncOpenAI
        api_key = os.getenv("NVIDIA_API_KEY")
        if not api_key:
            raise RuntimeError("NVIDIA_API_KEY not set")
        _nvidia_client = AsyncOpenAI(api_key=api_key, base_url=_normalize_nvidia_base_url(_NVIDIA_BASE_URL))
    return _nvidia_client


def _get_cerebras_client():
    global _cerebras_client
    if _cerebras_client is None:
        from openai import AsyncOpenAI
        api_key = os.getenv("CEREBRAS_API_KEY")
        if not api_key:
            raise RuntimeError("CEREBRAS_API_KEY not set")
        _cerebras_client = AsyncOpenAI(api_key=api_key, base_url=_CEREBRAS_BASE_URL)
    return _cerebras_client


def _get_groq_client():
    global _groq_client
    if _groq_client is None:
        from openai import AsyncOpenAI
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY not set")
        _groq_client = AsyncOpenAI(api_key=api_key, base_url=_GROQ_BASE_URL)
    return _groq_client


def _get_openrouter_client():
    global _openrouter_client
    if _openrouter_client is None:
        from openai import AsyncOpenAI
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY not set")
        _openrouter_client = AsyncOpenAI(
            api_key=api_key,
            base_url=_OPENROUTER_BASE_URL,
            default_headers={"HTTP-Referer": "https://github.com/clai-talos", "X-Title": "Clai-TALOS"},
        )
    return _openrouter_client


def _get_mistral_client():
    global _mistral_client
    if _mistral_client is None:
        from openai import AsyncOpenAI
        api_key = os.getenv("MISTRAL_API_KEY")
        if not api_key:
            raise RuntimeError("MISTRAL_API_KEY not set")
        _mistral_client = AsyncOpenAI(api_key=api_key, base_url=_MISTRAL_BASE_URL)
    return _mistral_client


def _get_ollama_client():
    global _ollama_client
    if _ollama_client is None:
        from openai import AsyncOpenAI
        _ollama_client = AsyncOpenAI(
            api_key="ollama",
            base_url=_OLLAMA_BASE_URL,
            timeout=_model_timeout_for_speed("normal", "ollama"),
            max_retries=0,
        )
    return _ollama_client


def _safe_json_loads(raw, default=None):
    if default is None:
        default = {}
    if not raw:
        return default
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else default
    except (json.JSONDecodeError, TypeError):
        return default


def _tools_to_openai(tools: list[dict] | None) -> list[dict] | None:
    return tools


# ---------------------------------------------------------------------------
# Streaming support
#
# `call_model` accepts an optional `on_delta` coroutine. Providers backed by the
# OpenAI-compatible async client stream their response and hand each delta to
# that callback so the UI can show the model's thinking/output as it arrives.
# Providers without streaming support still emit one final delta so callers can
# treat every provider uniformly.
# ---------------------------------------------------------------------------

_STREAM_SINK: ContextVar[Any] = ContextVar("talos_stream_sink", default=None)


async def _emit_delta(kind: str, text: str) -> None:
    """Push a streamed fragment to the sink installed by `call_model`, if any."""
    if not text:
        return
    sink = _STREAM_SINK.get()
    if sink is None:
        return
    try:
        await sink(kind, text)
    except Exception:
        logger.debug("stream sink raised; dropping delta", exc_info=True)


def _streaming_wanted() -> bool:
    return _STREAM_SINK.get() is not None


def _reasoning_from_delta(delta: Any) -> str:
    for attr in ("reasoning_content", "reasoning", "thinking"):
        value = getattr(delta, attr, None)
        if isinstance(value, str) and value:
            return value
    return ""


def _build_openai_result(reply_text: str, tool_calls: list[dict], reasoning: str = "") -> dict:
    return {
        "content": reply_text,
        "reasoning": reasoning,
        "tool_calls": tool_calls,
        "message": {
            "role": "assistant",
            "content": reply_text,
            "tool_calls": [
                {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}}
                for tc in tool_calls
            ] if tool_calls else None,
        },
    }


async def _aclose_stream_chain(stream: Any) -> None:
    """Finalize the SSE decoder/iterator once a stream is done with.

    Matters most when the stream is abandoned part-way (a timeout cancels the
    round), where the generators would otherwise sit suspended until garbage
    collection rather than being closed on the spot.
    """
    for attr in ("_iterator", "_decoder"):
        candidate = getattr(stream, attr, None)
        aclose = getattr(candidate, "aclose", None)
        if aclose is None:
            continue
        try:
            await aclose()
        except Exception:
            logger.debug("Ignoring error while closing %s of stream", attr, exc_info=True)


async def _stream_openai_chat(client: Any, kwargs: dict[str, Any]) -> dict:
    """Run an OpenAI-compatible chat completion in streaming mode.

    Accumulates content, reasoning, and tool-call argument fragments into the
    same result shape the non-streaming path returns.
    """
    stream_kwargs = dict(kwargs)
    stream_kwargs["stream"] = True

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    # index -> {"id", "name", "arguments"} accumulated across deltas
    partial_tools: dict[int, dict[str, str]] = {}

    stream = await client.chat.completions.create(**stream_kwargs)
    # `async with` guarantees the HTTP response is released even if the caller
    # is cancelled mid-stream, which otherwise leaks pooled connections.
    try:
        async with stream:
            async for chunk in stream:
                if not getattr(chunk, "choices", None):
                    continue
                delta = getattr(chunk.choices[0], "delta", None)
                if delta is None:
                    continue

                reasoning_piece = _reasoning_from_delta(delta)
                if reasoning_piece:
                    reasoning_parts.append(reasoning_piece)
                    await _emit_delta("reasoning", reasoning_piece)

                piece = getattr(delta, "content", None)
                if piece:
                    content_parts.append(piece)
                    await _emit_delta("content", piece)

                for tc in (getattr(delta, "tool_calls", None) or []):
                    idx = getattr(tc, "index", 0) or 0
                    slot = partial_tools.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                    tc_id = getattr(tc, "id", None)
                    if tc_id:
                        slot["id"] = tc_id
                    fn = getattr(tc, "function", None)
                    if fn is None:
                        continue
                    fn_name = getattr(fn, "name", None)
                    if fn_name:
                        slot["name"] = fn_name
                        await _emit_delta("tool", f"{fn_name}(")
                    fn_args = getattr(fn, "arguments", None)
                    if fn_args:
                        slot["arguments"] += fn_args
                        await _emit_delta("tool_args", fn_args)

    finally:
        await _aclose_stream_chain(stream)

    tool_calls = []
    for idx in sorted(partial_tools):
        slot = partial_tools[idx]
        if not slot["name"]:
            continue
        tool_calls.append({
            "id": slot["id"] or f"call_{idx}",
            "name": slot["name"],
            "arguments": _safe_json_loads(slot["arguments"], {}),
        })

    reply_text = "".join(content_parts)
    return _build_openai_result(reply_text, tool_calls, "".join(reasoning_parts))


def _collect_openai_response(response: Any) -> dict:
    """Normalize a non-streaming OpenAI-compatible response."""
    choice = response.choices[0]
    reply_text = choice.message.content or ""
    reasoning = ""
    for attr in ("reasoning_content", "reasoning"):
        value = getattr(choice.message, attr, None)
        if isinstance(value, str) and value:
            reasoning = value
            break

    tool_calls = []
    for tc in (getattr(choice.message, "tool_calls", None) or []):
        fn = getattr(tc, "function", None)
        name = getattr(fn, "name", None)
        if not name:
            continue
        tool_calls.append({
            "id": getattr(tc, "id", None) or f"tool_{len(tool_calls)}",
            "name": name,
            "arguments": _safe_json_loads(getattr(fn, "arguments", None), {}),
        })

    return _build_openai_result(reply_text, tool_calls, reasoning)


async def _openai_chat(client: Any, kwargs: dict[str, Any]) -> dict:
    """Stream when a sink is listening, otherwise use the plain request path."""
    if _streaming_wanted():
        try:
            return await _stream_openai_chat(client, kwargs)
        except Exception:
            logger.warning("Streaming call failed, retrying without streaming", exc_info=True)
    response = await client.chat.completions.create(**kwargs)
    result = _collect_openai_response(response)
    await _emit_delta("content", result.get("content", ""))
    return result




def _tools_to_anthropic(tools: list[dict] | None) -> list[dict] | None:
    if not tools:
        return None
    anthropic_tools = []
    for tool in tools:
        if tool.get("type") == "function":
            fn = tool["function"]
            anthropic_tools.append({
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })
    return anthropic_tools


async def call_openai(
    model_id: str,
    messages: list[dict],
    tools: list[dict] | None,
    runtime_profile: dict[str, Any] | None = None,
) -> dict:
    client = _get_openai_client()
    kwargs: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = _tools_to_openai(tools)
        kwargs["tool_choice"] = "auto"

    return await _openai_chat(client, kwargs)


async def call_anthropic(
    model_id: str,
    messages: list[dict],
    tools: list[dict] | None,
    runtime_profile: dict[str, Any] | None = None,
) -> dict:
    client = _get_anthropic_client()

    system_text = None
    filtered_messages = []
    for msg in messages:
        if msg.get("role") == "system":
            system_text = msg.get("content", "")
        elif msg.get("role") == "tool":
            filtered_messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id", ""),
                        "content": msg.get("content", ""),
                    }
                ],
            })
        elif msg.get("role") == "assistant" and msg.get("tool_calls"):
            content_blocks = []
            if msg.get("content"):
                content_blocks.append({"type": "text", "text": msg["content"]})
            for tc in msg["tool_calls"]:
                content_blocks.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["function"]["name"],
                    "input": _safe_json_loads(tc["function"].get("arguments", "{}"), {}),
                })
            filtered_messages.append({"role": "assistant", "content": content_blocks})
        else:
            filtered_messages.append(msg)

    kwargs: dict[str, Any] = {
        "model": model_id,
        "messages": filtered_messages,
        "max_tokens": 4096,
    }
    if system_text:
        kwargs["system"] = system_text
    if tools:
        kwargs["tools"] = _tools_to_anthropic(tools)
        kwargs["tool_choice"] = {"type": "auto"}

    reply_text = ""
    tool_calls = []

    if _streaming_wanted():
        async with client.messages.stream(**kwargs) as stream:
            async for event in stream.text_stream:
                reply_text += event
                await _emit_delta("content", event)
            response = await stream.get_final_message()
        for block in response.content:
            if block.type == "tool_use":
                await _emit_delta("tool", f"{block.name}(")
                tool_calls.append({
                    "id": block.id,
                    "name": block.name,
                    "arguments": block.input if isinstance(block.input, dict) else _safe_json_loads(getattr(block, "input", None), {}),
                })
        return _build_openai_result(reply_text, tool_calls)

    response = await client.messages.create(**kwargs)

    for block in response.content:
        if block.type == "text":
            reply_text += block.text
        elif block.type == "tool_use":
            tool_calls.append({
                "id": block.id,
                "name": block.name,
                "arguments": block.input if isinstance(block.input, dict) else _safe_json_loads(getattr(block, "input", None), {}),
            })

    await _emit_delta("content", reply_text)
    return _build_openai_result(reply_text, tool_calls)


# Gemini validates tool parameters against its own OpenAPI subset and rejects
# anything else outright, so JSON-Schema keywords other providers accept have to
# be translated or dropped before the call. This is exactly the field set of the
# Gemini API's Schema type; the SDK's Schema model is wider because it also
# covers Vertex AI, so keys like additionalProperties, defs and ref pass
# client-side validation and are then rejected by the REST endpoint.
_GEMINI_SCHEMA_KEYS = frozenset({
    "type", "format", "title", "description", "nullable", "default", "example",
    "enum", "items", "properties", "required", "propertyOrdering", "anyOf",
    "pattern", "minimum", "maximum", "minItems", "maxItems",
    "minLength", "maxLength", "minProperties", "maxProperties",
})


# What an untyped or unconstrained value becomes: Gemini has no "any" type, but
# a union of the scalars covers what a tool argument can actually hold.
_GEMINI_ANY_SCALAR = {"anyOf": [{"type": "string"}, {"type": "number"}, {"type": "boolean"}]}


def _sanitize_gemini_schema(schema: Any) -> Any:
    if isinstance(schema, list):
        return [_sanitize_gemini_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in ("oneOf", "anyOf"):
            # Gemini only knows anyOf; for its purposes here the two are
            # interchangeable, since both just widen the accepted type.
            variants = [_sanitize_gemini_schema(v) for v in value if isinstance(v, dict)]
            if variants:
                out["anyOf"] = variants
        elif key == "allOf":
            # No equivalent exists, so flatten the branches into one schema.
            for variant in value:
                if isinstance(variant, dict):
                    out.update(_sanitize_gemini_schema(variant))
        elif key == "properties" and isinstance(value, dict):
            out["properties"] = {k: _sanitize_gemini_schema(v) for k, v in value.items()}
        elif key == "items":
            out["items"] = _sanitize_gemini_schema(value)
        elif key in _GEMINI_SCHEMA_KEYS:
            out[key] = value

    # JSON Schema allows a list of types ("string" or null); Gemini takes a
    # single type plus the nullable flag.
    declared = out.get("type")
    if isinstance(declared, list):
        named = [str(t) for t in declared if str(t).lower() != "null"]
        if len(named) < len(declared):
            out["nullable"] = True
        if len(named) == 1:
            out["type"] = named[0]
        else:
            out.pop("type", None)
            out.setdefault("anyOf", [{"type": t} for t in named] or _GEMINI_ANY_SCALAR["anyOf"])

    # A schema described only by a union carries no type of its own, and Gemini
    # rejects the two side by side.
    if "anyOf" in out:
        out.pop("type", None)

    # An enum is only meaningful against a type, and Gemini expects a string one.
    if "enum" in out and not out.get("type"):
        out["type"] = "string"

    # Gemini requires every ARRAY to say what it holds; a bare {"type": "array"}
    # is rejected with "items: missing field".
    if str(out.get("type", "")).lower() == "array" and not isinstance(out.get("items"), dict):
        out["items"] = dict(_GEMINI_ANY_SCALAR)

    return out


async def call_gemini(
    model_id: str,
    messages: list[dict],
    tools: list[dict] | None,
    runtime_profile: dict[str, Any] | None = None,
) -> dict:
    client = _get_gemini_client()

    contents = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if role == "system":
            contents.append({"role": "user", "parts": [{"text": f"System: {content}"}]})
        elif role == "user":
            if isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            parts.append({"text": item.get("text", "")})
                        elif item.get("type") == "image_url":
                            img_url = item.get("image_url", {}).get("url", "")
                            if img_url.startswith("data:"):
                                import base64
                                mime_end = img_url.index(";base64,")
                                mime_type = img_url[5:mime_end]
                                b64_data = img_url[mime_end + 8:]
                                parts.append({"inline_data": {"mime_type": mime_type, "data": b64_data}})
                contents.append({"role": "user", "parts": parts})
            else:
                contents.append({"role": "user", "parts": [{"text": content}]})
        elif role == "assistant":
            parts = []
            if content:
                parts.append({"text": content})
            msg_tool_calls = msg.get("tool_calls")
            if msg_tool_calls:
                for tc in msg_tool_calls:
                    fn = tc.get("function", {})
                    args = fn.get("arguments", "{}")
                    try:
                        args_dict = json.loads(args) if isinstance(args, str) else args
                    except (json.JSONDecodeError, TypeError):
                        args_dict = {}
                    parts.append({
                        "function_call": {
                            "name": fn.get("name", ""),
                            "args": args_dict,
                        }
                    })
            if parts:
                contents.append({"role": "model", "parts": parts})
        elif role == "tool":
            contents.append({"role": "user", "parts": [{"text": f"Tool result: {content}"}]})

    from google.genai import types

    gemini_tools = None
    if tools:
        function_decls = []
        for tool in tools:
            if tool.get("type") == "function":
                fn = tool["function"]
                parameters = _sanitize_gemini_schema(fn.get("parameters") or {})
                # Gemini refuses an OBJECT whose properties map is empty, so a
                # no-argument tool has to omit its parameters entirely.
                if not parameters.get("properties"):
                    parameters = None
                try:
                    function_decls.append(
                        types.FunctionDeclaration(
                            name=fn["name"],
                            description=fn.get("description", ""),
                            parameters=parameters,
                        )
                    )
                except Exception:
                    # One unusable tool schema should not take down the whole
                    # turn; drop the tool and let the rest through.
                    logger.warning(
                        "Skipping tool %r for Gemini: its parameter schema was rejected",
                        fn.get("name", "?"),
                        exc_info=True,
                    )
        if function_decls:
            gemini_tools = [types.Tool(function_declarations=function_decls)]
    
    config = types.GenerateContentConfig(
        temperature=0.7,
        max_output_tokens=2048,
        tools=gemini_tools,
    )

    response = await asyncio.to_thread(
        client.models.generate_content,
        model=model_id,
        contents=contents,
        config=config,
    )

    text = ""
    tool_calls = []
    if hasattr(response, 'candidates') and response.candidates:
        candidate = response.candidates[0]
        if hasattr(candidate, 'content') and candidate.content:
            for part in candidate.content.parts:
                if hasattr(part, 'text') and part.text:
                    text += part.text
                elif hasattr(part, 'function_call') and part.function_call:
                    fc = part.function_call
                    tool_calls.append({
                        "id": f"gemini_{len(tool_calls)}",
                        "name": fc.name,
                        "arguments": dict(fc.args) if fc.args else {},
                    })

    await _emit_delta("content", text)
    return _build_openai_result(text, tool_calls)


async def call_zhipu(
    model_id: str,
    messages: list[dict],
    tools: list[dict] | None,
    runtime_profile: dict[str, Any] | None = None,
) -> dict:
    client = _get_zhipu_client()

    response = await asyncio.to_thread(
        client.chat.completions.create,
        model=model_id,
        messages=messages,
        tools=tools,
        tool_choice="auto" if tools else None,
    )

    result = _collect_openai_response(response)
    await _emit_delta("reasoning", result.get("reasoning", ""))
    await _emit_delta("content", result.get("content", ""))
    return result


async def call_nvidia(
    model_id: str,
    messages: list[dict],
    tools: list[dict] | None,
    runtime_profile: dict[str, Any] | None = None,
) -> dict:
    client = _get_nvidia_client()
    model_id = _normalize_nvidia_model_id(model_id)
    runtime = runtime_profile or {}
    speed_mode = _normalize_speed_mode(runtime.get("speed_mode"))
    reasoning_enabled = bool(runtime.get("reasoning_enabled", True))

    max_tokens_by_speed = {
        "quick": 1024,
        "fast": 2048,
        "normal": 4096,
    }

    kwargs: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
        "temperature": 1,
        "top_p": 1,
        "max_tokens": max_tokens_by_speed.get(speed_mode, 4096),
    }
    if model_id.startswith("z-ai/glm4"):
        kwargs["extra_body"] = {
            "chat_template_kwargs": {
                "enable_thinking": reasoning_enabled,
                "clear_thinking": not reasoning_enabled,
            }
        }
    if tools:
        kwargs["tools"] = _tools_to_openai(tools)
        kwargs["tool_choice"] = "auto"

    try:
        return await _openai_chat(client, kwargs)
    except Exception as exc:
        lowered = str(exc).lower()
        if "404" not in lowered and "not found" not in lowered:
            raise

        last_exc = exc
        for fallback_model in _pick_nvidia_fallback_models(model_id):
            retry_kwargs = dict(kwargs)
            retry_kwargs["model"] = fallback_model
            if fallback_model.startswith("z-ai/glm4"):
                retry_kwargs["extra_body"] = {
                    "chat_template_kwargs": {
                        "enable_thinking": reasoning_enabled,
                        "clear_thinking": not reasoning_enabled,
                    }
                }
            else:
                retry_kwargs.pop("extra_body", None)
            try:
                return await _openai_chat(client, retry_kwargs)
            except Exception as retry_exc:
                last_exc = retry_exc
        raise last_exc


async def call_cerebras(
    model_id: str,
    messages: list[dict],
    tools: list[dict] | None,
    runtime_profile: dict[str, Any] | None = None,
) -> dict:
    client = _get_cerebras_client()
    kwargs: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = _tools_to_openai(tools)
        kwargs["tool_choice"] = "auto"

    return await _openai_chat(client, kwargs)


async def call_groq(
    model_id: str,
    messages: list[dict],
    tools: list[dict] | None,
    runtime_profile: dict[str, Any] | None = None,
) -> dict:
    client = _get_groq_client()
    kwargs: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = _tools_to_openai(tools)
        kwargs["tool_choice"] = "auto"

    return await _openai_chat(client, kwargs)


async def call_openrouter(
    model_id: str,
    messages: list[dict],
    tools: list[dict] | None,
    runtime_profile: dict[str, Any] | None = None,
) -> dict:
    client = _get_openrouter_client()
    kwargs: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = _tools_to_openai(tools)
        kwargs["tool_choice"] = "auto"

    return await _openai_chat(client, kwargs)


async def call_mistral(
    model_id: str,
    messages: list[dict],
    tools: list[dict] | None,
    runtime_profile: dict[str, Any] | None = None,
) -> dict:
    client = _get_mistral_client()
    kwargs: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = _tools_to_openai(tools)
        kwargs["tool_choice"] = "auto"

    return await _openai_chat(client, kwargs)


def _env_float(name: str) -> float | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        logger.warning(f"{name}={raw!r} is not a number; ignoring")
        return None


def _env_int(name: str) -> int | None:
    value = _env_float(name)
    return int(value) if value is not None else None


# --- Ollama --------------------------------------------------------------
#
# Ollama exposes an OpenAI-compatible endpoint, but that endpoint silently drops
# every Ollama-specific field: num_ctx, keep_alive and the rest never reach the
# runner. Models therefore load in the default 4096-token window, and TALOS
# spends roughly 9k tokens on its system prompt and tool schemas alone -- so the
# conversation history is truncated away before the model ever reads it, and the
# assistant answers every message as if it were the first. The native /api/chat
# endpoint honours those fields, so that is what we talk to.


async def ollama_options(model_id: str = "") -> dict[str, Any]:
    """Ollama generation options: Settings, plus an auto-sized context window."""
    import ollama_setup
    options = await ollama_setup.generation_options(model_id)
    temperature = _env_float("OLLAMA_TEMPERATURE")
    if temperature is not None:
        options["temperature"] = temperature
    max_tokens = _env_int("OLLAMA_MAX_TOKENS")
    if max_tokens:
        options["num_predict"] = max_tokens
    return options


def _ollama_content_and_images(content: Any) -> tuple[str, list[str]]:
    """Flatten OpenAI-style content into native Ollama text + base64 images."""
    if isinstance(content, str):
        return content, []

    texts: list[str] = []
    images: list[str] = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            texts.append(str(part.get("text") or ""))
        elif part.get("type") == "image_url":
            url = str((part.get("image_url") or {}).get("url") or "")
            if url.startswith("data:"):
                images.append(url.split(",", 1)[-1])
            elif url:
                images.append(url)
    return "\n".join(t for t in texts if t), images


def _ollama_messages(messages: list[dict]) -> list[dict]:
    """Convert the OpenAI-shaped conversation into native Ollama messages.

    Extra keys have to be stripped: stored history carries `image_b64`, and
    tool results carry `tool_call_id`, neither of which Ollama understands.
    """
    converted: list[dict] = []
    names_by_id: dict[str, str] = {}

    for msg in messages:
        role = str(msg.get("role") or "user")
        text, images = _ollama_content_and_images(msg.get("content"))
        out: dict[str, Any] = {"role": role, "content": text}
        if images:
            out["images"] = images

        if role == "assistant" and msg.get("tool_calls"):
            calls = []
            for tc in msg["tool_calls"] or []:
                fn = (tc or {}).get("function") or {}
                name = fn.get("name")
                if not name:
                    continue
                args = fn.get("arguments")
                if isinstance(args, str):
                    args = _safe_json_loads(args, {})
                calls.append({"function": {"name": name, "arguments": args or {}}})
                if tc.get("id"):
                    names_by_id[tc["id"]] = name
            if calls:
                out["tool_calls"] = calls

        if role == "tool":
            name = names_by_id.get(str(msg.get("tool_call_id") or ""))
            if name:
                out["tool_name"] = name

        converted.append(out)
    return converted


def _ollama_tool_calls(message: dict, start: int = 0) -> list[dict]:
    calls = []
    for offset, tc in enumerate(message.get("tool_calls") or []):
        fn = (tc or {}).get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        args = fn.get("arguments")
        if isinstance(args, str):
            args = _safe_json_loads(args, {})
        calls.append({
            "id": tc.get("id") or f"call_{start + offset}",
            "name": name,
            "arguments": args if isinstance(args, dict) else {},
        })
    return calls


async def _ollama_native_chat(root: str, payload: dict[str, Any]) -> dict:
    """POST to Ollama's native /api/chat, streaming when a sink is listening."""
    import httpx

    url = f"{root}/api/chat"
    # No read timeout: `call_model` already wraps the whole call in a deadline,
    # and a local model can be quiet for a long time while it thinks.
    timeout = httpx.Timeout(connect=10.0, read=None, write=60.0, pool=10.0)

    if not payload.get("stream"):
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        message = data.get("message") or {}
        content = str(message.get("content") or "")
        result = _build_openai_result(
            content, _ollama_tool_calls(message), str(message.get("thinking") or "")
        )
        await _emit_delta("content", content)
        return result

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict] = []

    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, json=payload) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", "replace")
                raise RuntimeError(f"Ollama returned {resp.status_code}: {body[:400]}")

            async for line in resp.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("error"):
                    raise RuntimeError(str(event["error"]))

                message = event.get("message") or {}
                thinking = message.get("thinking")
                if thinking:
                    reasoning_parts.append(thinking)
                    await _emit_delta("reasoning", thinking)
                piece = message.get("content")
                if piece:
                    content_parts.append(piece)
                    await _emit_delta("content", piece)

                for call in _ollama_tool_calls(message, len(tool_calls)):
                    tool_calls.append(call)
                    await _emit_delta("tool", f"{call['name']}(")
                    await _emit_delta("tool_args", json.dumps(call["arguments"]))

                if event.get("done"):
                    break

    return _build_openai_result("".join(content_parts), tool_calls, "".join(reasoning_parts))


async def _call_ollama_compat(model_id: str, messages: list[dict], tools: list[dict] | None) -> dict:
    """Last-resort path for an endpoint that only speaks the OpenAI dialect.

    Ollama's own options do not survive this route, so the model runs with
    whatever context window it was loaded with.
    """
    kwargs: dict[str, Any] = {"model": model_id, "messages": messages}
    temperature = _env_float("OLLAMA_TEMPERATURE")
    if temperature is not None:
        kwargs["temperature"] = temperature
    max_tokens = _env_int("OLLAMA_MAX_TOKENS")
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    if tools:
        kwargs["tools"] = _tools_to_openai(tools)
        kwargs["tool_choice"] = "auto"
    return await _openai_chat(_get_ollama_client(), kwargs)


async def call_ollama(
    model_id: str,
    messages: list[dict],
    tools: list[dict] | None,
    runtime_profile: dict[str, Any] | None = None,
) -> dict:
    import ollama_setup

    root = ollama_setup.native_base_url(_OLLAMA_BASE_URL)
    profile = runtime_profile or {}

    payload: dict[str, Any] = {
        "model": model_id,
        "messages": _ollama_messages(messages),
        "stream": _streaming_wanted(),
        "keep_alive": ollama_setup.keep_alive_value(),
    }
    options = await ollama_options(model_id)
    if options:
        payload["options"] = options
    if tools:
        payload["tools"] = _tools_to_openai(tools)
    # Passing `think` to a model that has no thinking mode is an error, so only
    # send it when the model advertises the capability.
    if await ollama_setup.supports_thinking(model_id, root):
        payload["think"] = bool(profile.get("reasoning_enabled", True))

    try:
        return await _ollama_native_chat(root, payload)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "Ollama native /api/chat failed; falling back to the OpenAI-compatible "
            "endpoint, where num_ctx and keep_alive are ignored",
            exc_info=True,
        )
        return await _call_ollama_compat(model_id, messages, tools)


_CALLERS = {
    "openai": call_openai,
    "anthropic": call_anthropic,
    "gemini": call_gemini,
    "zhipu": call_zhipu,
    "nvidia": call_nvidia,
    "cerebras": call_cerebras,
    "groq": call_groq,
    "openrouter": call_openrouter,
    "mistral": call_mistral,
    "ollama": call_ollama,
}


_MAIN_MODEL_PREFERENCES = [
    "o3",
    "gpt-4.1",
    "gpt-4o",
    "claude-sonnet-4-20250514",
    "claude-3-5-sonnet-20241022",
    "gemini-2.5-pro",
    "glm-5",
    "glm-4",
]

_IMAGE_MODEL_PREFERENCES = [
    "gpt-4o",
    "gpt-4.1",
    "claude-sonnet-4-20250514",
    "gemini-2.5-pro",
    "glm-4v",
]


_IMAGE_MODEL_HINTS = (
    "gpt-4o",
    "gpt-4.1",
    "o3",
    "o4-",
    "claude-sonnet-4",
    "claude-3-5",
    "gemini-2.5",
    "gemini-2.0",
    "gemini-1.5",
    "glm-4v",
    "pixtral",
)


def _is_image_model(model_id: str) -> bool:
    lowered = model_id.lower()
    if "vision" in lowered or "multimodal" in lowered:
        return True
    return any(hint in lowered for hint in _IMAGE_MODEL_HINTS)


def _provider_enabled(provider: str) -> bool:
    if provider == "ollama":
        return bool(os.getenv("OLLAMA_MODEL", "").strip())
    cfg = _PROVIDERS.get(provider, {})
    env_key = cfg.get("env_key")
    if not env_key:
        return False
    return bool(os.getenv(env_key, "").strip())


def _available_models() -> list[str]:
    models = list_provider_models()
    filtered = []
    for model_id in models:
        provider, _ = resolve_model(model_id)
        if _provider_enabled(provider):
            filtered.append(model_id)
    return filtered


def _pick_preferred(preferences: list[str], candidates: list[str], fallback: str) -> str:
    if not candidates:
        return fallback
    lowered = {m.lower(): m for m in candidates}
    for pref in preferences:
        hit = lowered.get(pref.lower())
        if hit:
            return hit
    return candidates[0]


_MODEL_CALL_TIMEOUT_S = int(os.getenv("MODEL_CALL_TIMEOUT_S", "120"))


def _normalize_speed_mode(speed_mode: str | None) -> str:
    mode = str(speed_mode or "").strip().lower()
    if mode in {"quick", "fast", "normal"}:
        return mode
    return "normal"


def _normalize_runtime_profile(
    speed_mode: str | None = None,
    reasoning_enabled: bool | None = None,
) -> dict[str, Any]:
    return {
        "speed_mode": _normalize_speed_mode(speed_mode),
        "reasoning_enabled": True if reasoning_enabled is None else bool(reasoning_enabled),
    }


def _model_timeout_for_speed(speed_mode: str, provider: str = "") -> int:
    if provider == "ollama":
        # Local models run on the user's own hardware and are far slower than
        # hosted APIs, so they get their own (longer) budget.
        raw = os.getenv("OLLAMA_TIMEOUT_S", "").strip()
        try:
            return max(30, min(int(raw), 3600)) if raw else 600
        except ValueError:
            return 600
    base = max(30, min(_MODEL_CALL_TIMEOUT_S, 600))
    if speed_mode == "quick":
        return max(30, min(base, 70))
    if speed_mode == "fast":
        return max(30, min(base, 85))
    return base

async def call_model(
    model: str,
    messages: list[dict],
    tools: list[dict] | None,
    speed_mode: str | None = None,
    reasoning_enabled: bool | None = None,
    on_delta: Any = None,
) -> dict:
    """Call a model, optionally streaming fragments to `on_delta`.

    `on_delta` is an async callable `(kind, text)` where kind is one of
    "content", "reasoning", "tool", or "tool_args".
    """
    provider, model_id = resolve_model(model)
    caller = _CALLERS.get(provider)
    if not caller:
        return {"content": f"Unknown provider: {provider}", "tool_calls": [], "message": None}
    if not _provider_enabled(provider):
        cfg = _PROVIDERS.get(provider, {})
        env_key = cfg.get("env_key", "API_KEY")
        return {"content": f"Model \"{model}\" requires provider \"{provider}\", but {env_key} is not set. Add your API key in Settings to use this model.", "tool_calls": [], "message": None}

    runtime_profile = _normalize_runtime_profile(speed_mode=speed_mode, reasoning_enabled=reasoning_enabled)
    sink_token = _STREAM_SINK.set(on_delta)
    try:
        timeout = _model_timeout_for_speed(runtime_profile["speed_mode"], provider)
        return await asyncio.wait_for(caller(model_id, messages, tools, runtime_profile), timeout=timeout)
    except asyncio.TimeoutError:
        logger.error(
            f"{provider} call timed out after {timeout}s for model {model_id} "
            f"(speed={runtime_profile['speed_mode']}, reasoning={runtime_profile['reasoning_enabled']})"
        )
        return {"content": f"Model call to {provider}/{model_id} timed out after {timeout}s. The API may be overloaded.", "tool_calls": [], "message": None}
    except Exception as e:
        logger.exception(f"{provider} call failed for model {model_id}")
        if provider == "nvidia":
            err = str(e)
            if "404" in err.lower() or "not found" in err.lower():
                return {
                    "content": (
                        "NVIDIA returned 404 for the current model/request. "
                        "Use `nvidia/z-ai/glm4.7` and keep `NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1`."
                    ),
                    "tool_calls": [],
                    "message": None,
                }
        return {"content": f"Error communicating with {provider}: {e}", "tool_calls": [], "message": None}
    finally:
        _STREAM_SINK.reset(sink_token)


async def call_model_simple(
    model: str,
    system: str,
    prompt: str,
    speed_mode: str | None = None,
    reasoning_enabled: bool | None = None,
) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    result = await call_model(
        model,
        messages,
        None,
        speed_mode=speed_mode,
        reasoning_enabled=reasoning_enabled,
    )
    return result.get("content", "")


def _fetch_gemini_models(api_key: str) -> list[str]:
    import google.genai as genai
    client = genai.Client(api_key=api_key)
    models = []
    seen = set()
    for m in client.models.list():
        name = str(getattr(m, "name", "") or "").removeprefix("models/")
        if not name or name in seen:
            continue
        lowered = name.lower()
        if not any(p in lowered for p in _PROVIDERS["gemini"]["patterns"]):
            continue
        # Embedding and image-generation endpoints show up in the same listing
        # but cannot answer a chat turn.
        if "embedding" in lowered or "aqa" in lowered:
            continue
        actions = getattr(m, "supported_actions", None)
        if actions and "generateContent" not in actions:
            continue
        seen.add(name)
        models.append(name)
    return models if models else list(_PROVIDERS["gemini"]["models"].values())


def _fetch_openai_models(api_key: str) -> list[str]:
    import httpx
    models = []
    try:
        r = httpx.get(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        r.raise_for_status()
        for m in r.json().get("data", []):
            mid = m["id"]
            if any(p in mid for p in _PROVIDERS["openai"]["patterns"]):
                models.append(mid)
    except Exception:
        pass
    return models


def _fetch_anthropic_models(api_key: str) -> list[str]:
    import httpx
    models = []
    try:
        r = httpx.get(
            "https://api.anthropic.com/v1/models",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            timeout=10,
        )
        r.raise_for_status()
        for m in r.json().get("data", []):
            mid = m.get("id", "")
            if any(p in mid for p in _PROVIDERS["anthropic"]["patterns"]):
                models.append(mid)
    except Exception:
        pass
    if not models:
        models = [
            "claude-sonnet-4-20250514",
            "claude-3-5-sonnet-20241022",
            "claude-3-5-haiku-20241022",
            "claude-3-opus-20240229",
        ]
    return models


def _fetch_zhipu_models(api_key: str) -> list[str]:
    import httpx
    models = []
    try:
        r = httpx.get(
            f"{_CLIENT_BASE_URL}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        r.raise_for_status()
        models = [m["id"] for m in r.json().get("data", [])]
    except Exception:
        pass
    return models if models else list(_PROVIDERS["zhipu"]["models"].values())


def _fetch_nvidia_models(api_key: str) -> list[str]:
    import httpx
    models = []
    try:
        r = httpx.get(
            _nvidia_endpoint(_NVIDIA_MODELS_PATH),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        r.raise_for_status()
        for m in r.json().get("data", []):
            mid = m["id"]
            models.append(mid)
    except Exception:
        pass
    return models if models else list(_PROVIDERS["nvidia"]["models"].values())


def _pick_nvidia_fallback_models(current_model_id: str) -> list[str]:
    current = _normalize_nvidia_model_id(current_model_id)
    preferred = ["z-ai/glm4.7", "z-ai/glm5"]

    api_key = os.getenv("NVIDIA_API_KEY", "").strip()
    available: list[str] = []
    if api_key:
        try:
            available = _fetch_nvidia_models(api_key)
        except Exception:
            available = []

    out: list[str] = []
    for model in preferred:
        if model != current and (not available or model in available):
            out.append(model)

    for model in available:
        if model != current and model not in out:
            out.append(model)

    return out


def _fetch_cerebras_models(api_key: str) -> list[str]:
    import httpx
    models = []
    try:
        r = httpx.get(
            f"{_CEREBRAS_BASE_URL}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        r.raise_for_status()
        for m in r.json().get("data", []):
            mid = m["id"]
            models.append(mid)
    except Exception:
        pass
    return models if models else list(_PROVIDERS["cerebras"]["models"].values())


def _fetch_groq_models(api_key: str) -> list[str]:
    import httpx
    models = []
    try:
        r = httpx.get(
            f"{_GROQ_BASE_URL}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        r.raise_for_status()
        for m in r.json().get("data", []):
            mid = m["id"]
            models.append(mid)
    except Exception:
        pass
    return models if models else list(_PROVIDERS["groq"]["models"].values())


def _fetch_openrouter_models(api_key: str) -> list[str]:
    import httpx
    models = []
    try:
        r = httpx.get(
            f"{_OPENROUTER_BASE_URL}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        r.raise_for_status()
        for m in r.json().get("data", []):
            mid = m.get("id", "")
            if mid:
                models.append(mid)
    except Exception:
        pass
    return models if models else list(_PROVIDERS["openrouter"]["models"].values())


def _fetch_mistral_models(api_key: str) -> list[str]:
    import httpx
    models = []
    seen = set()
    try:
        r = httpx.get(
            f"{_MISTRAL_BASE_URL}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        r.raise_for_status()
        for m in r.json().get("data", []):
            mid = m.get("id", "")
            # Mistral lists embedding, moderation and OCR endpoints alongside
            # the chat models, and repeats ids across their aliases.
            if not mid or mid in seen:
                continue
            lowered = mid.lower()
            if any(skip in lowered for skip in ("embed", "moderation", "ocr")):
                continue
            capabilities = m.get("capabilities") or {}
            if capabilities and not capabilities.get("completion_chat", True):
                continue
            seen.add(mid)
            models.append(mid)
    except Exception:
        pass
    return models if models else list(_PROVIDERS["mistral"]["models"].values())


def _fetch_ollama_models(api_key: str) -> list[str]:
    import httpx
    models = []
    try:
        r = httpx.get(
            "http://localhost:11434/api/tags",
            timeout=5,
        )
        r.raise_for_status()
        for m in r.json().get("models", []):
            name = m.get("name", "").replace(":latest", "")
            if name:
                models.append(name)
    except Exception:
        pass
    return models


def fetch_provider_models(provider: str, api_key: str) -> dict:
    fetchers = {
        "gemini": _fetch_gemini_models,
        "openai": _fetch_openai_models,
        "anthropic": _fetch_anthropic_models,
        "zhipu": _fetch_zhipu_models,
        "nvidia": _fetch_nvidia_models,
        "cerebras": _fetch_cerebras_models,
        "groq": _fetch_groq_models,
        "openrouter": _fetch_openrouter_models,
        "mistral": _fetch_mistral_models,
        "ollama": _fetch_ollama_models,
    }
    fetcher = fetchers.get(provider)
    if not fetcher:
        return {"models": [], "image_models": []}
    try:
        models = fetcher(api_key)
    except Exception:
        models = list(_PROVIDERS.get(provider, {}).get("models", {}).values())
    image_models = [m for m in models if _is_image_model(m)]
    if not image_models:
        image_models = models
    return {"models": sorted(models), "image_models": sorted(set(image_models))}


def list_provider_models() -> list[str]:
    import httpx
    models = []

    api_key = os.getenv("ZHIPUAI_API_KEY", "")
    if api_key:
        try:
            r = httpx.get(
                f"{_CLIENT_BASE_URL}/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10,
            )
            r.raise_for_status()
            models.extend(m["id"] for m in r.json().get("data", []))
        except Exception:
            pass

    if os.getenv("GEMINI_API_KEY"):
        try:
            client = _get_gemini_client()
            available = client.models.list()
            for m in available:
                name = getattr(m, "name", "").removeprefix("models/")
                if any(p in name.lower() for p in _PROVIDERS["gemini"]["patterns"]):
                    if name not in models:
                        models.append(name)
        except Exception:
            pass

    if os.getenv("OPENAI_API_KEY"):
        try:
            client = _get_openai_client()
            response = asyncio.get_event_loop().run_until_complete(
                client.models.list()
            )
            for m in response.data:
                if m.id not in models:
                    models.append(m.id)
        except Exception:
            try:
                import httpx
                r = httpx.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"},
                    timeout=10,
                )
                r.raise_for_status()
                for m in r.json().get("data", []):
                    mid = m["id"]
                    if mid not in models and any(p in mid for p in _PROVIDERS["openai"]["patterns"]):
                        models.append(mid)
            except Exception:
                pass

    if os.getenv("ANTHROPIC_API_KEY"):
        anthropic_models = [
            "claude-sonnet-4-20250514",
            "claude-3-5-sonnet-20241022",
            "claude-3-5-haiku-20241022",
            "claude-3-opus-20240229",
        ]
        for m in anthropic_models:
            if m not in models:
                models.append(m)

    if os.getenv("NVIDIA_API_KEY"):
        try:
            r = httpx.get(
                _nvidia_endpoint(_NVIDIA_MODELS_PATH),
                headers={"Authorization": f"Bearer {os.getenv('NVIDIA_API_KEY')}"},
                timeout=10,
            )
            r.raise_for_status()
            for m in r.json().get("data", []):
                mid = m["id"]
                if mid not in models:
                    models.append(mid)
        except Exception:
            pass

    if os.getenv("CEREBRAS_API_KEY"):
        try:
            r = httpx.get(
                f"{_CEREBRAS_BASE_URL}/models",
                headers={"Authorization": f"Bearer {os.getenv('CEREBRAS_API_KEY')}"},
                timeout=10,
            )
            r.raise_for_status()
            for m in r.json().get("data", []):
                mid = m["id"]
                if mid not in models:
                    models.append(mid)
        except Exception:
            pass

    if os.getenv("GROQ_API_KEY"):
        try:
            r = httpx.get(
                f"{_GROQ_BASE_URL}/models",
                headers={"Authorization": f"Bearer {os.getenv('GROQ_API_KEY')}"},
                timeout=10,
            )
            r.raise_for_status()
            for m in r.json().get("data", []):
                mid = m["id"]
                if mid not in models:
                    models.append(mid)
        except Exception:
            pass

    if os.getenv("OPENROUTER_API_KEY"):
        try:
            r = httpx.get(
                f"{_OPENROUTER_BASE_URL}/models",
                headers={"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}"},
                timeout=10,
            )
            r.raise_for_status()
            for m in r.json().get("data", []):
                mid = m.get("id", "")
                if mid and mid not in models:
                    models.append(mid)
        except Exception:
            pass

    if os.getenv("MISTRAL_API_KEY"):
        for m in _fetch_mistral_models(os.getenv("MISTRAL_API_KEY", "")):
            if m not in models:
                models.append(m)

    ollama_model = os.getenv("OLLAMA_MODEL", "").strip()
    if ollama_model:
        tagged = ollama_model if ollama_model.startswith("ollama/") else "ollama/" + ollama_model
        if tagged not in models:
            models.append(tagged)

    return models if models else list(get_all_model_aliases().values())


def list_models_with_provider() -> list[str]:
    result = []
    seen = set()

    api_key = os.getenv("ZHIPUAI_API_KEY", "")
    if api_key:
        try:
            import httpx
            r = httpx.get(f"{_CLIENT_BASE_URL}/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=10)
            r.raise_for_status()
            for m in r.json().get("data", []):
                mid = m["id"]
                tagged = "zhipu/" + mid
                if tagged not in seen:
                    seen.add(tagged)
                    result.append(tagged)
        except Exception:
            pass

    if os.getenv("GEMINI_API_KEY"):
        for m in _PROVIDERS["gemini"]["models"].values():
            tagged = "gemini/" + m
            if tagged not in seen:
                seen.add(tagged)
                result.append(tagged)

    if os.getenv("OPENAI_API_KEY"):
        for m in _PROVIDERS["openai"]["models"].values():
            tagged = "openai/" + m
            if tagged not in seen:
                seen.add(tagged)
                result.append(tagged)

    if os.getenv("ANTHROPIC_API_KEY"):
        for m in _PROVIDERS["anthropic"]["models"].values():
            tagged = "anthropic/" + m
            if tagged not in seen:
                seen.add(tagged)
                result.append(tagged)

    if os.getenv("NVIDIA_API_KEY"):
        try:
            import httpx
            r = httpx.get(_nvidia_endpoint(_NVIDIA_MODELS_PATH), headers={"Authorization": f"Bearer {os.getenv('NVIDIA_API_KEY')}"}, timeout=10)
            r.raise_for_status()
            for m in r.json().get("data", []):
                mid = m["id"]
                tagged = "nvidia/" + mid
                if tagged not in seen:
                    seen.add(tagged)
                    result.append(tagged)
        except Exception:
            pass

    if os.getenv("CEREBRAS_API_KEY"):
        try:
            import httpx
            r = httpx.get(f"{_CEREBRAS_BASE_URL}/models", headers={"Authorization": f"Bearer {os.getenv('CEREBRAS_API_KEY')}"}, timeout=10)
            r.raise_for_status()
            for m in r.json().get("data", []):
                mid = m["id"]
                tagged = "cerebras/" + mid
                if tagged not in seen:
                    seen.add(tagged)
                    result.append(tagged)
        except Exception:
            pass

    if os.getenv("GROQ_API_KEY"):
        try:
            import httpx
            r = httpx.get(f"{_GROQ_BASE_URL}/models", headers={"Authorization": f"Bearer {os.getenv('GROQ_API_KEY')}"}, timeout=10)
            r.raise_for_status()
            for m in r.json().get("data", []):
                mid = m["id"]
                tagged = "groq/" + mid
                if tagged not in seen:
                    seen.add(tagged)
                    result.append(tagged)
        except Exception:
            pass

    if os.getenv("OPENROUTER_API_KEY"):
        try:
            import httpx
            r = httpx.get(f"{_OPENROUTER_BASE_URL}/models", headers={"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}"}, timeout=10)
            r.raise_for_status()
            for m in r.json().get("data", []):
                mid = m.get("id", "")
                if mid:
                    tagged = "openrouter/" + mid
                    if tagged not in seen:
                        seen.add(tagged)
                        result.append(tagged)
        except Exception:
            pass

    if os.getenv("MISTRAL_API_KEY"):
        for m in _fetch_mistral_models(os.getenv("MISTRAL_API_KEY", "")):
            tagged = "mistral/" + m
            if tagged not in seen:
                seen.add(tagged)
                result.append(tagged)

    ollama_model = os.getenv("OLLAMA_MODEL", "").strip()
    if ollama_model:
        tagged = "ollama/" + ollama_model
        if tagged not in seen:
            seen.add(tagged)
            result.append(tagged)

    return result if result else [p + "/" + m for p, m in get_all_model_aliases().items()]


def list_image_models() -> list[str]:
    models = list_provider_models()
    image_models = [m for m in models if _is_image_model(m)]
    if not image_models:
        return models
    return sorted(set(image_models))


def best_main_model() -> str:
    candidates = _available_models()
    return _pick_preferred(_MAIN_MODEL_PREFERENCES, candidates, "glm-5")


def best_image_model() -> str:
    candidates = [m for m in _available_models() if _is_image_model(m)]
    return _pick_preferred(_IMAGE_MODEL_PREFERENCES, candidates, best_main_model())
