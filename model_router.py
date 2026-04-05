import os
import json
import logging
import asyncio
import time
from typing import Any
from dotenv import load_dotenv

load_dotenv()

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
            "gemini15flash": "gemini-1.5-flash",
            "gemini20flash": "gemini-2.0-flash",
            "gemini25pro": "gemini-2.5-pro",
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
_openrouter_client = None
_ollama_client = None

_NVIDIA_BASE_URL = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
_CEREBRAS_BASE_URL = os.getenv("CEREBRAS_BASE_URL", "https://api.cerebras.ai/v1")
_OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
_OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")

_CLIENT_BASE_URL = os.getenv("CLIENT_BASE_URL", "https://api.z.ai/api/coding/paas/v4")


def resolve_model(model: str) -> tuple[str, str]:
    if "/" in model and not model.startswith("http"):
        provider_hint, model_id = model.split("/", 1)
        provider_hint = provider_hint.lower().strip()
        for provider_name in _PROVIDERS:
            if provider_name == provider_hint:
                return provider_name, model_id

    model_lower = model.lower().strip()
    for provider_name, provider_cfg in _PROVIDERS.items():
        if model_lower in provider_cfg["models"]:
            return provider_name, provider_cfg["models"][model_lower]
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
    global _openai_client, _anthropic_client, _gemini_client, _zhipu_client, _nvidia_client, _cerebras_client, _openrouter_client, _ollama_client
    global _CLIENT_BASE_URL, _NVIDIA_BASE_URL, _CEREBRAS_BASE_URL, _OPENROUTER_BASE_URL, _OLLAMA_BASE_URL
    _openai_client = None
    _anthropic_client = None
    _gemini_client = None
    _zhipu_client = None
    _nvidia_client = None
    _cerebras_client = None
    _openrouter_client = None
    _ollama_client = None
    load_dotenv(override=True)
    _CLIENT_BASE_URL = os.getenv("CLIENT_BASE_URL", "https://api.z.ai/api/coding/paas/v4")
    _NVIDIA_BASE_URL = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
    _CEREBRAS_BASE_URL = os.getenv("CEREBRAS_BASE_URL", "https://api.cerebras.ai/v1")
    _OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    _OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")


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
        _nvidia_client = AsyncOpenAI(api_key=api_key, base_url=_NVIDIA_BASE_URL)
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


def _get_ollama_client():
    global _ollama_client
    if _ollama_client is None:
        from openai import AsyncOpenAI
        _ollama_client = AsyncOpenAI(api_key="ollama", base_url=_OLLAMA_BASE_URL)
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


async def call_openai(model_id: str, messages: list[dict], tools: list[dict] | None) -> dict:
    client = _get_openai_client()
    kwargs: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = _tools_to_openai(tools)
        kwargs["tool_choice"] = "auto"

    response = await client.chat.completions.create(**kwargs)
    choice = response.choices[0]
    reply_text = choice.message.content or ""

    tool_calls = []
    if choice.message.tool_calls:
        for tc in choice.message.tool_calls:
            tool_calls.append({
                "id": tc.id,
                "name": tc.function.name,
                "arguments": _safe_json_loads(tc.function.arguments, {}),
            })

    return {
        "content": reply_text,
        "tool_calls": tool_calls,
        "message": {
            "role": "assistant",
            "content": reply_text,
            "tool_calls": [
                {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}}
                for tc in tool_calls
            ] if tool_calls else None,
        }
    }


async def call_anthropic(model_id: str, messages: list[dict], tools: list[dict] | None) -> dict:
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

    response = await client.messages.create(**kwargs)

    reply_text = ""
    tool_calls = []

    for block in response.content:
        if block.type == "text":
            reply_text += block.text
        elif block.type == "tool_use":
            tool_calls.append({
                "id": block.id,
                "name": block.name,
                "arguments": block.input if isinstance(block.input, dict) else _safe_json_loads(getattr(block, "input", None), {}),
            })

    return {
        "content": reply_text,
        "tool_calls": tool_calls,
        "message": {
            "role": "assistant",
            "content": reply_text,
            "tool_calls": [
                {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}}
                for tc in tool_calls
            ] if tool_calls else None,
        }
    }


async def call_gemini(model_id: str, messages: list[dict], tools: list[dict] | None) -> dict:
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
                function_decls.append(
                    types.FunctionDeclaration(
                        name=fn["name"],
                        description=fn.get("description", ""),
                        parameters=fn.get("parameters", {"type": "object", "properties": {}}),
                    )
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

    return {
        "content": text,
        "tool_calls": tool_calls,
        "message": {
            "role": "assistant",
            "content": text,
            "tool_calls": [
                {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}}
                for tc in tool_calls
            ] if tool_calls else None,
        }
    }


async def call_zhipu(model_id: str, messages: list[dict], tools: list[dict] | None) -> dict:
    client = _get_zhipu_client()

    response = await asyncio.to_thread(
        client.chat.completions.create,
        model=model_id,
        messages=messages,
        tools=tools,
        tool_choice="auto" if tools else None,
    )

    choice = response.choices[0]
    reply_text = choice.message.content or ""

    tool_calls = []
    if hasattr(choice.message, 'tool_calls') and choice.message.tool_calls:
        for tc in choice.message.tool_calls:
            fn = getattr(tc, "function", None)
            name = getattr(fn, "name", None)
            if not name:
                continue
            tool_calls.append({
                "id": getattr(tc, "id", f"tool_{len(tool_calls)}"),
                "name": name,
                "arguments": _safe_json_loads(getattr(fn, "arguments", None), {}),
            })

    return {
        "content": reply_text,
        "tool_calls": tool_calls,
        "message": {
            "role": "assistant",
            "content": reply_text,
            "tool_calls": [
                {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}}
                for tc in tool_calls
            ] if tool_calls else None,
        }
    }


async def call_nvidia(model_id: str, messages: list[dict], tools: list[dict] | None) -> dict:
    client = _get_nvidia_client()
    kwargs: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
        "temperature": 1,
        "top_p": 1,
        "max_tokens": 16384,
    }
    if tools:
        kwargs["tools"] = _tools_to_openai(tools)
        kwargs["tool_choice"] = "auto"

    response = await client.chat.completions.create(**kwargs)
    choice = response.choices[0]
    reply_text = choice.message.content or ""

    tool_calls = []
    if choice.message.tool_calls:
        for tc in choice.message.tool_calls:
            tool_calls.append({
                "id": tc.id,
                "name": tc.function.name,
                "arguments": _safe_json_loads(tc.function.arguments, {}),
            })

    return {
        "content": reply_text,
        "tool_calls": tool_calls,
        "message": {
            "role": "assistant",
            "content": reply_text,
            "tool_calls": [
                {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}}
                for tc in tool_calls
            ] if tool_calls else None,
        }
    }


async def call_cerebras(model_id: str, messages: list[dict], tools: list[dict] | None) -> dict:
    client = _get_cerebras_client()
    kwargs: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = _tools_to_openai(tools)
        kwargs["tool_choice"] = "auto"

    response = await client.chat.completions.create(**kwargs)
    choice = response.choices[0]
    reply_text = choice.message.content or ""

    tool_calls = []
    if choice.message.tool_calls:
        for tc in choice.message.tool_calls:
            tool_calls.append({
                "id": tc.id,
                "name": tc.function.name,
                "arguments": _safe_json_loads(tc.function.arguments, {}),
            })

    return {
        "content": reply_text,
        "tool_calls": tool_calls,
        "message": {
            "role": "assistant",
            "content": reply_text,
            "tool_calls": [
                {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}}
                for tc in tool_calls
            ] if tool_calls else None,
        }
    }


async def call_openrouter(model_id: str, messages: list[dict], tools: list[dict] | None) -> dict:
    client = _get_openrouter_client()
    kwargs: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = _tools_to_openai(tools)
        kwargs["tool_choice"] = "auto"

    response = await client.chat.completions.create(**kwargs)
    choice = response.choices[0]
    reply_text = choice.message.content or ""

    tool_calls = []
    if choice.message.tool_calls:
        for tc in choice.message.tool_calls:
            tool_calls.append({
                "id": tc.id,
                "name": tc.function.name,
                "arguments": _safe_json_loads(tc.function.arguments, {}),
            })

    return {
        "content": reply_text,
        "tool_calls": tool_calls,
        "message": {
            "role": "assistant",
            "content": reply_text,
            "tool_calls": [
                {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}}
                for tc in tool_calls
            ] if tool_calls else None,
        }
    }


async def call_ollama(model_id: str, messages: list[dict], tools: list[dict] | None) -> dict:
    client = _get_ollama_client()
    kwargs: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = _tools_to_openai(tools)
        kwargs["tool_choice"] = "auto"

    response = await client.chat.completions.create(**kwargs)
    choice = response.choices[0]
    reply_text = choice.message.content or ""

    tool_calls = []
    if choice.message.tool_calls:
        for tc in choice.message.tool_calls:
            tool_calls.append({
                "id": tc.id,
                "name": tc.function.name,
                "arguments": _safe_json_loads(tc.function.arguments, {}),
            })

    return {
        "content": reply_text,
        "tool_calls": tool_calls,
        "message": {
            "role": "assistant",
            "content": reply_text,
            "tool_calls": [
                {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}}
                for tc in tool_calls
            ] if tool_calls else None,
        }
    }


_CALLERS = {
    "openai": call_openai,
    "anthropic": call_anthropic,
    "gemini": call_gemini,
    "zhipu": call_zhipu,
    "nvidia": call_nvidia,
    "cerebras": call_cerebras,
    "openrouter": call_openrouter,
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

async def call_model(model: str, messages: list[dict], tools: list[dict] | None) -> dict:
    provider, model_id = resolve_model(model)
    caller = _CALLERS.get(provider)
    if not caller:
        return {"content": f"Unknown provider: {provider}", "tool_calls": [], "message": None}
    if not _provider_enabled(provider):
        cfg = _PROVIDERS.get(provider, {})
        env_key = cfg.get("env_key", "API_KEY")
        return {"content": f"Model \"{model}\" requires provider \"{provider}\", but {env_key} is not set. Add your API key in Settings to use this model.", "tool_calls": [], "message": None}
    try:
        timeout = max(30, min(_MODEL_CALL_TIMEOUT_S, 600))
        return await asyncio.wait_for(caller(model_id, messages, tools), timeout=timeout)
    except asyncio.TimeoutError:
        logger.error(f"{provider} call timed out after {timeout}s for model {model_id}")
        return {"content": f"Model call to {provider}/{model_id} timed out after {timeout}s. The API may be overloaded.", "tool_calls": [], "message": None}
    except Exception as e:
        logger.exception(f"{provider} call failed for model {model_id}")
        return {"content": f"Error communicating with {provider}: {e}", "tool_calls": [], "message": None}


async def call_model_simple(model: str, system: str, prompt: str) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    result = await call_model(model, messages, None)
    return result.get("content", "")


def _fetch_gemini_models(api_key: str) -> list[str]:
    import google.genai as genai
    client = genai.Client(api_key=api_key)
    models = []
    available = client.models.list()
    for m in available:
        name = getattr(m, "name", "").removeprefix("models/")
        if any(p in name.lower() for p in _PROVIDERS["gemini"]["patterns"]):
            models.append(name)
    return models


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
            f"{_NVIDIA_BASE_URL}/models",
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
        "openrouter": _fetch_openrouter_models,
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
                f"{_NVIDIA_BASE_URL}/models",
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

    ollama_model = os.getenv("OLLAMA_MODEL", "").strip()
    if ollama_model:
        if ollama_model not in models:
            models.append(ollama_model)

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
            r = httpx.get(f"{_NVIDIA_BASE_URL}/models", headers={"Authorization": f"Bearer {os.getenv('NVIDIA_API_KEY')}"}, timeout=10)
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
