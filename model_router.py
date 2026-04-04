import os
import json
import logging
import asyncio
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
}

_openai_client = None
_anthropic_client = None
_gemini_client = None
_zhipu_client = None

_CLIENT_BASE_URL = os.getenv("CLIENT_BASE_URL", "https://api.z.ai/api/coding/paas/v4")


def resolve_model(model: str) -> tuple[str, str]:
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
    global _openai_client, _anthropic_client, _gemini_client, _zhipu_client
    global _CLIENT_BASE_URL
    _openai_client = None
    _anthropic_client = None
    _gemini_client = None
    _zhipu_client = None
    load_dotenv(override=True)
    _CLIENT_BASE_URL = os.getenv("CLIENT_BASE_URL", "https://api.z.ai/api/coding/paas/v4")


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
            contents.append({"role": "user", "parts": [{"text": content}]})
        elif role == "assistant":
            contents.append({"role": "model", "parts": [{"text": content}]})
        elif role == "tool":
            contents.append({"role": "user", "parts": [{"text": f"Tool result: {content}"}]})

    config = {
        "temperature": 0.7,
        "max_output_tokens": 2048,
    }

    response = await asyncio.to_thread(
        client.models.generate_content,
        model=model_id,
        contents=contents,
        generation_config=config,
    )

    text = response.text if hasattr(response, 'text') else ""

    return {
        "content": text,
        "tool_calls": [],
        "message": {"role": "assistant", "content": text},
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


_CALLERS = {
    "openai": call_openai,
    "anthropic": call_anthropic,
    "gemini": call_gemini,
    "zhipu": call_zhipu,
}


async def call_model(model: str, messages: list[dict], tools: list[dict] | None) -> dict:
    provider, model_id = resolve_model(model)
    caller = _CALLERS.get(provider)
    if not caller:
        return {"content": f"Unknown provider: {provider}", "tool_calls": [], "message": None}
    try:
        return await caller(model_id, messages, tools)
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

    return models if models else list(get_all_model_aliases().values())
