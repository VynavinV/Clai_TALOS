import os
import httpx
import asyncio
import json
import logging
from typing import Callable, Awaitable
from dotenv import load_dotenv
from zhipuai import ZhipuAI
import google.genai as genai
import db
import memory
import terminal_tools
import environment
import cron_jobs
import websearch
import firecrawl

load_dotenv()

logger = logging.getLogger("talos.ai")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.join(SCRIPT_DIR, "tools")

_MODELS = {
    "glm4": "glm-4",
    "glm4v": "glm-4v",
    "glm5": "glm-5",
    "glm5turbo": "glm-5-turbo",
    "charglm3": "charglm-3",
    "gemini15flash": "gemini-1.5-flash",
    "gemini20flash": "gemini-2.0-flash",
    "gemini25pro": "gemini-2.5-pro",
}

_client: ZhipuAI | None = None
_gemini_client: genai.Client | None = None

_CLIENT_BASE_URL = "https://api.z.ai/api/coding/paas/v4"
_MAX_TOOL_ROUNDS = 5
_MAX_TOOL_CALLS_PER_ROUND = 8
_MAX_COMMAND_TIMEOUT = 120
_MAX_WORKFLOW_STEPS = 12
_MAX_SUBAGENT_TOOL_ROUNDS = 3
_MAX_SUBAGENT_TOOL_CALLS_PER_ROUND = 4

SendFunc = Callable[[str], Awaitable[None]]


def _get_zhipu_client() -> ZhipuAI:
    global _client
    if _client is None:
        api_key = os.getenv("ZHIPUAI_API_KEY")
        if not api_key:
            raise RuntimeError("ZHIPUAI_API_KEY not set in .env")
        _client = ZhipuAI(api_key=api_key, base_url=_CLIENT_BASE_URL)
    return _client


def _get_gemini_client() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set in .env")
        _gemini_client = genai.Client(api_key=api_key)
    return _gemini_client


def reload_clients():
    global _client, _gemini_client
    _client = None
    _gemini_client = None
    load_dotenv(override=True)


def list_models() -> list[str]:
    api_key = os.getenv("ZHIPUAI_API_KEY", "")
    r = httpx.get(
        f"{_CLIENT_BASE_URL}/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=10,
    )
    r.raise_for_status()
    return [m["id"] for m in r.json().get("data", [])]


def _load_tools_guide() -> str:
    guides = []
    if os.path.isdir(TOOLS_DIR):
        for filename in sorted(os.listdir(TOOLS_DIR)):
            if filename.endswith(".md"):
                filepath = os.path.join(TOOLS_DIR, filename)
                try:
                    with open(filepath, "r") as f:
                        content = f.read().strip()
                    tool_name = filename[:-3]
                    guides.append(f"### {tool_name}\n{content}")
                except Exception:
                    pass
    return "\n\n".join(guides) if guides else ""


def _build_system(user_id: int, current_message: str = "", include_memories: bool = True) -> str | None:
    parts = []
    prompt = db.read_system_prompt()
    if prompt:
        parts.append(prompt)

    tools_guide = _load_tools_guide()
    if tools_guide:
        parts.append(f"[Available Tools]\n{tools_guide}")

    env_context = environment.get_environment_context()
    if env_context:
        parts.append(f"[Environment]\n{env_context}")
    tg_format = environment.get_telegram_formatting_guide()
    if tg_format:
        parts.append(tg_format)

    parts.append(
        "[Orchestrator Instructions]\n"
        "You are the orchestrator. When a task benefits from decomposition, use spawn_subagent "
        "to delegate focused subtasks. Each subagent can message the user directly on Telegram "
        "with intros, updates, and conclusions via the send_telegram_message tool. "
        "You can spawn multiple subagents in a single response - they will run in PARALLEL. "
        "After all subagents complete, synthesize their results into a final coherent response. "
        "Keep the user informed: if spawning subagents, tell the user what you're delegating and why."
    )

    summary = db.get_summary(user_id)
    if summary:
        parts.append(f"[Previous conversation summary]\n{summary}")

    if include_memories and current_message:
        relevant_memories = memory.get_relevant_memories(user_id, current_message)
        if relevant_memories:
            memories_str = memory.format_memories_for_context(relevant_memories)
            parts.append(memories_str)

    return "\n\n".join(parts) if parts else None


def _build_subagent_system(user_id: int, role: str, task: str, context: str = "") -> str | None:
    parts = []
    prompt = db.read_system_prompt()
    if prompt:
        parts.append(prompt)
    parts.append(
        "You are a subagent delegated by the main TALOS orchestrator. "
        "Stay narrowly focused on the assigned task. Do not spawn other subagents.\n\n"
        "IMPORTANT: You have access to send_telegram_message. Use it to communicate "
        "directly with the user:\n"
        "1. Send a brief intro when you start (what you're about to do)\n"
        "2. Send updates if the work takes multiple steps\n"
        "3. Send your conclusion/result when done\n"
        "Keep messages concise and useful. Sign off with your role in brackets so the user "
        "knows which subagent is talking, e.g. [researcher] or [executor]."
    )
    parts.append(f"[Subagent role]\n{role or 'general'}")
    parts.append(f"[Delegated task]\n{task}")
    if context.strip():
        parts.append(f"[Delegation context]\n{context.strip()}")

    env_context = environment.get_environment_context()
    if env_context:
        parts.append(f"[Environment]\n{env_context}")
    tg_format = environment.get_telegram_formatting_guide()
    if tg_format:
        parts.append(tg_format)

    summary = db.get_summary(user_id)
    if summary:
        parts.append(f"[Previous conversation summary]\n{summary}")

    return "\n\n".join(parts) if parts else None


def _safe_json_loads(raw: str | None, default: dict | None = None) -> dict:
    if default is None:
        default = {}
    if not raw:
        return default
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else default
    except (json.JSONDecodeError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

def _get_all_tools(include_subagent: bool = True, include_telegram: bool = False):
    tools = [
        {
            "type": "function",
            "function": {
                "name": "execute_command",
                "description": "Execute a terminal command. The environment (native/docker/firejail) is configured by the user. Check the [Environment] section in your context to understand your access level.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "The terminal command to execute"},
                        "timeout": {"type": "number", "description": "Maximum execution time in seconds (default: 30)"}
                    },
                    "required": ["command"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "execute_workflow",
                "description": "Execute multiple commands in sequence with optional conditions.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "steps": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "command": {"type": "string", "description": "Command to execute"},
                                    "timeout": {"type": "number", "description": "Timeout for this step in seconds"},
                                    "condition": {"type": "string", "description": "Condition: 'success', 'failure', or 'output_contains'"}
                                },
                                "required": ["command"]
                            }
                        }
                    },
                    "required": ["steps"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "schedule_cron",
                "description": "Schedule a cron job that runs a terminal command on a cron schedule.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Short name for the job"},
                        "schedule": {"type": "string", "description": "Cron schedule (e.g. '*/5 * * * *')"},
                        "command": {"type": "string", "description": "Command to run"},
                        "timezone": {"type": "string", "description": "Timezone name (default: UTC)"}
                    },
                    "required": ["name", "schedule", "command"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "list_cron",
                "description": "List cron jobs for the current user.",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "remove_cron",
                "description": "Remove a cron job by id.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "integer", "description": "Cron job id"}
                    },
                    "required": ["job_id"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "save_memory",
                "description": "Save important information to long-term memory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "The information to remember"},
                        "category": {"type": "string", "description": "Category (e.g., 'preferences', 'projects', 'facts')"},
                        "importance": {"type": "number", "description": "Importance 1-10 (default 5)"}
                    },
                    "required": ["content"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "search_memories",
                "description": "Search through stored memories.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search terms"}
                    },
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "list_memories",
                "description": "List all stored memories or filter by category.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "description": "Filter by category (optional)"},
                        "limit": {"type": "number", "description": "Max results (default 20)"}
                    }
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "delete_memory",
                "description": "Delete a memory by its ID.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "memory_id": {"type": "number", "description": "ID of the memory to delete"}
                    },
                    "required": ["memory_id"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "update_memory",
                "description": "Update an existing memory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "memory_id": {"type": "number", "description": "ID of the memory to update"},
                        "content": {"type": "string", "description": "New content"},
                        "category": {"type": "string", "description": "New category"},
                        "importance": {"type": "number", "description": "New importance (1-10)"}
                    },
                    "required": ["memory_id"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web for current information. Use for news, real-time data, documentation, or fact-checking.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "The search query"},
                        "scope": {"type": "string", "description": "Search scope filter (e.g., 'news', 'academic')"},
                        "location": {"type": "string", "description": "Location context for localized results"},
                        "recent_days": {"type": "number", "description": "Only include results from the last N days"}
                    },
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "scrape_url",
                "description": "Scrape and extract content from a web page. Returns clean markdown by default.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "The URL to scrape"},
                        "formats": {
                            "type": "array",
                            "items": {"type": "string", "enum": ["markdown", "html", "links", "screenshot"]},
                            "description": "Output formats (default: ['markdown'])"
                        },
                        "only_main_content": {"type": "boolean", "description": "Extract only main content, excluding nav/footer (default: true)"},
                        "timeout": {"type": "number", "description": "Timeout in milliseconds (default: 30000)"},
                        "max_age": {"type": "number", "description": "Cache max age in ms (default: 172800000 = 2 days)"}
                    },
                    "required": ["url"]
                }
            }
        }
    ]

    if include_subagent:
        tools.append({
            "type": "function",
            "function": {
                "name": "spawn_subagent",
                "description": (
                    "Delegate a focused task to a bounded subagent that can message the user "
                    "directly on Telegram. Multiple spawn_subagent calls in one response run "
                    "in PARALLEL. Use for research, planning, review, or execution subtasks."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "role": {"type": "string", "description": "Short role label: researcher, planner, reviewer, executor, etc."},
                        "task": {"type": "string", "description": "The specific task to delegate"},
                        "context": {"type": "string", "description": "Extra context or constraints"}
                    },
                    "required": ["task"]
                }
            }
        })

    if include_telegram:
        tools.append({
            "type": "function",
            "function": {
                "name": "send_telegram_message",
                "description": (
                    "Send a message directly to the user on Telegram. Use for intros, "
                    "progress updates, and conclusions. Keep messages concise."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "The message to send"}
                    },
                    "required": ["message"]
                }
            }
        })
        tools.append({
            "type": "function",
            "function": {
                "name": "send_voice_message",
                "description": (
                    "Send a voice message to the user on Telegram. Converts text to speech. "
                    "Use when the user prefers audio or for longer responses. Keep text under 500 chars."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "The text to convert to speech (max 500 chars)"}
                    },
                    "required": ["text"]
                }
            }
        })

    return tools


# ---------------------------------------------------------------------------
# Tool execution (fully async)
# ---------------------------------------------------------------------------

async def _execute_tool_call(
    tool_name: str,
    tool_args: dict,
    user_id: int,
    send_func: SendFunc | None = None,
    allow_subagent: bool = True,
) -> str:
    try:
        if tool_name == "execute_command":
            command = str(tool_args.get("command", "")).strip()
            timeout = tool_args.get("timeout", 30)
            if not isinstance(timeout, (int, float)):
                timeout = 30
            timeout = max(1, min(int(timeout), _MAX_COMMAND_TIMEOUT))
            if not command:
                return json.dumps({"error": "No command provided"})
            result = await terminal_tools.execute_command(command, timeout=timeout)
            return json.dumps(result, indent=2)

        elif tool_name == "execute_workflow":
            steps = tool_args.get("steps")
            if not isinstance(steps, list) or not steps:
                return json.dumps({"error": "No steps provided"})
            cleaned = []
            for step in steps[:_MAX_WORKFLOW_STEPS]:
                if not isinstance(step, dict):
                    continue
                cmd = str(step.get("command", "")).strip()
                if not cmd:
                    continue
                t = step.get("timeout", 30)
                if not isinstance(t, (int, float)):
                    t = 30
                cleaned.append({"command": cmd, "timeout": max(1, min(int(t), _MAX_COMMAND_TIMEOUT)), "condition": step.get("condition")})
            if not cleaned:
                return json.dumps({"error": "No valid steps"})
            result = await terminal_tools.execute_workflow(cleaned)
            return json.dumps(result, indent=2)

        elif tool_name == "schedule_cron":
            name = str(tool_args.get("name", "")).strip()
            schedule = str(tool_args.get("schedule", "")).strip()
            command = str(tool_args.get("command", "")).strip()
            tz = str(tool_args.get("timezone", "UTC")).strip() or "UTC"
            if not name or not schedule or not command:
                return json.dumps({"error": "name, schedule, and command are required"})
            result = cron_jobs.schedule_job(user_id, name, schedule, command, tz)
            return json.dumps(result, indent=2)

        elif tool_name == "list_cron":
            return json.dumps({"jobs": cron_jobs.list_jobs(user_id)}, indent=2)

        elif tool_name == "remove_cron":
            job_id = tool_args.get("job_id")
            if job_id is None:
                return json.dumps({"error": "job_id is required"})
            return json.dumps({"ok": cron_jobs.remove_job(user_id, int(job_id))}, indent=2)

        elif tool_name == "save_memory":
            content = tool_args.get("content")
            if not content:
                return json.dumps({"error": "No content provided"})
            result = memory.save_memory(user_id, content, tool_args.get("category"), tool_args.get("importance", 5))
            return json.dumps({"success": True, "memory": result}, indent=2)

        elif tool_name == "search_memories":
            return json.dumps({"memories": memory.search_memories(user_id, tool_args.get("query", ""))}, indent=2)

        elif tool_name == "list_memories":
            return json.dumps({"memories": memory.list_memories(user_id, tool_args.get("category"), tool_args.get("limit", 20))}, indent=2)

        elif tool_name == "delete_memory":
            mid = tool_args.get("memory_id")
            if not mid:
                return json.dumps({"error": "No memory_id provided"})
            return json.dumps({"success": memory.delete_memory(user_id, mid)}, indent=2)

        elif tool_name == "update_memory":
            mid = tool_args.get("memory_id")
            if not mid:
                return json.dumps({"error": "No memory_id provided"})
            return json.dumps({"success": memory.update_memory(user_id, mid, content=tool_args.get("content"), category=tool_args.get("category"), importance=tool_args.get("importance"))}, indent=2)

        elif tool_name == "web_search":
            query = tool_args.get("query")
            scope = tool_args.get("scope", "")
            location = tool_args.get("location", "")
            recent_days = tool_args.get("recent_days", 0)
            result = websearch.web_search(query, scope=scope, location=location, recent_days=recent_days)
            return json.dumps(result, indent=2)

        elif tool_name == "scrape_url":
            url = tool_args.get("url")
            formats = tool_args.get("formats", ["markdown"])
            only_main_content = tool_args.get("only_main_content", True)
            timeout = tool_args.get("timeout", 30000)
            max_age = tool_args.get("max_age", 172800000)
            result = firecrawl.scrape_url(
                url,
                formats=formats,
                only_main_content=only_main_content,
                timeout=timeout,
                max_age=max_age,
            )
            return json.dumps(result, indent=2)

        elif tool_name == "send_telegram_message":
            message = str(tool_args.get("message", "")).strip()
            if not message:
                return json.dumps({"error": "No message provided"})
            if send_func:
                await send_func(message)
                return json.dumps({"sent": True})
            return json.dumps({"error": "No Telegram send function available"})

        elif tool_name == "send_voice_message":
            text = str(tool_args.get("text", "")).strip()
            if not text:
                return json.dumps({"error": "No text provided"})
            if len(text) > 500:
                text = text[:500]
            import voice
            audio_path = voice.text_to_speech(text)
            if not audio_path:
                return json.dumps({"error": "Failed to generate voice message"})
            if send_func:
                await send_func(audio_path, voice=True)
                return json.dumps({"sent": True, "audio_path": audio_path})
            return json.dumps({"error": "No Telegram send function available"})

        elif tool_name == "spawn_subagent":
            if not allow_subagent:
                return json.dumps({"error": "Nested subagent spawning is disabled"})
            role = str(tool_args.get("role", "general")).strip() or "general"
            task = str(tool_args.get("task", "")).strip()
            ctx = str(tool_args.get("context", "")).strip()
            if not task:
                return json.dumps({"error": "No task provided"})
            result = await _run_subagent(user_id, role, task, ctx, send_func)
            return json.dumps({"role": role, "task": task, "result": result}, indent=2)

        else:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})

    except Exception as e:
        logger.exception(f"Tool call error: {tool_name}")
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Agentic loop (multi-turn tool calling with parallel subagents)
# ---------------------------------------------------------------------------

async def _run_agent(
    model: str,
    prompt: str,
    system: str | None = None,
    history: list[dict] | None = None,
    tools: list[dict] | None = None,
    user_id: int | None = None,
    send_func: SendFunc | None = None,
    allow_subagent: bool = True,
    max_rounds: int = _MAX_TOOL_ROUNDS,
    max_calls_per_round: int = _MAX_TOOL_CALLS_PER_ROUND,
) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": prompt})

    if tools is None:
        tools = _get_all_tools(include_subagent=allow_subagent, include_telegram=send_func is not None)

    model_id = _MODELS.get(model.lower(), model)
    
    is_gemini = model_id.startswith("gemini")
    
    for _ in range(max_rounds):
        if is_gemini:
            response = await _call_gemini(model_id, messages, tools)
        else:
            response = await _call_zhipu(model_id, messages, tools)
        
        if not response.get("tool_calls"):
            return response.get("content", "I could not generate a response right now.")
        
        messages.append(response["message"])
        
        tool_calls = response["tool_calls"][:max_calls_per_round]
        
        subagent_calls = [tc for tc in tool_calls if tc["name"] == "spawn_subagent"]
        other_calls = [tc for tc in tool_calls if tc["name"] != "spawn_subagent"]
        
        results: dict[str, str] = {}
        
        for tc in other_calls:
            results[tc["id"]] = await _execute_tool_call(
                tc["name"], tc["arguments"],
                user_id if user_id is not None else 0,
                send_func=send_func,
                allow_subagent=allow_subagent,
            )
        
        if subagent_calls:
            async def _do_subagent(tc):
                r = await _execute_tool_call(
                    tc["name"], tc["arguments"],
                    user_id if user_id is not None else 0,
                    send_func=send_func,
                    allow_subagent=allow_subagent,
                )
                return tc["id"], r

            gathered = await asyncio.gather(
                *[_do_subagent(tc) for tc in subagent_calls],
                return_exceptions=True,
            )
            for item in gathered:
                if isinstance(item, Exception):
                    logger.error(f"Subagent failed: {item}")
                else:
                    results[item[0]] = item[1]
        
        for tc in tool_calls:
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": results.get(tc["id"], json.dumps({"error": "execution failed"})),
            })
    
    try:
        if is_gemini:
            response = await _call_gemini(model_id, messages, None)
        else:
            response = await _call_zhipu(model_id, messages, None)
        return response.get("content", "I could not generate a response right now.")
    except Exception:
        return "I ran out of processing rounds. Please try again."


async def _call_zhipu(model_id: str, messages: list[dict], tools: list[dict] | None) -> dict:
    try:
        response = await asyncio.to_thread(
            _get_zhipu_client().chat.completions.create,
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
    except Exception as e:
        logger.exception("ZhipuAI call failed")
        return {"content": f"Error communicating with AI: {e}", "tool_calls": [], "message": None}


async def _call_gemini(model_id: str, messages: list[dict], tools: list[dict] | None) -> dict:
    try:
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
    except Exception as e:
        logger.exception("Gemini call failed")
        return {"content": f"Error communicating with Gemini: {e}", "tool_calls": [], "message": None}


# ---------------------------------------------------------------------------
# Subagent runner
# ---------------------------------------------------------------------------

async def _run_subagent(
    user_id: int,
    role: str,
    task: str,
    context: str = "",
    send_func: SendFunc | None = None,
) -> str:
    model = db.get_model(user_id)
    system = _build_subagent_system(user_id, role, task, context)
    history = db.get_history(user_id)
    tools = _get_all_tools(include_subagent=False, include_telegram=send_func is not None)

    return await _run_agent(
        model, task,
        system=system,
        history=history,
        tools=tools,
        user_id=user_id,
        send_func=send_func,
        allow_subagent=False,
        max_rounds=_MAX_SUBAGENT_TOOL_ROUNDS,
        max_calls_per_round=_MAX_SUBAGENT_TOOL_CALLS_PER_ROUND,
    )


# ---------------------------------------------------------------------------
# Summarize (async)
# ---------------------------------------------------------------------------

async def _maybe_summarize(user_id: int, model: str) -> None:
    if db.count_messages(user_id) <= db.SUMMARY_THRESHOLD:
        return
    older = db.get_older_messages(user_id)
    if not older:
        return
    model_id = _MODELS.get(model.lower(), model)
    conversation = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in older)
    system_msg = (
        "You are a summarization assistant. Summarize the following conversation "
        "concisely in the same language used. Preserve key facts, decisions, names, "
        "numbers, code snippets, and any context needed to continue the conversation. "
        "Do not add commentary or meta-text. Output only the summary."
    )
    try:
        is_gemini = model_id.startswith("gemini")
        
        if is_gemini:
            client = _get_gemini_client()
            contents = [
                {"role": "user", "parts": [{"text": f"System: {system_msg}"}]},
                {"role": "user", "parts": [{"text": f"Summarize this conversation:\n\n{conversation}"}]},
            ]
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=model_id,
                contents=contents,
            )
            summary = response.text if hasattr(response, 'text') else ""
        else:
            response = await asyncio.to_thread(
                _get_zhipu_client().chat.completions.create,
                model=model_id,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": f"Summarize this conversation:\n\n{conversation}"},
                ],
            )
            summary = response.choices[0].message.content or ""
        
        db.set_summary(user_id, summary)
    except Exception:
        logger.exception("Summarization failed")


# ---------------------------------------------------------------------------
# Public entry point (async)
# ---------------------------------------------------------------------------

async def respond(user_id: int, text: str, send_func: SendFunc | None = None) -> str:
    model = db.get_model(user_id)
    history = db.get_history(user_id)
    system = _build_system(user_id, text)

    reply = await _run_agent(
        model, text,
        system=system,
        history=history,
        tools=_get_all_tools(include_subagent=True, include_telegram=False),
        user_id=user_id,
        send_func=send_func,
        allow_subagent=True,
    )

    db.add_message(user_id, "user", text)
    db.add_message(user_id, "assistant", reply)
    await _maybe_summarize(user_id, model)
    return reply
