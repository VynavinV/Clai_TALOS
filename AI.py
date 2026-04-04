import os
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Callable, Awaitable
from dotenv import load_dotenv
import db
import memory
import terminal_tools
import environment
import cron_jobs
import websearch
import firecrawl
import file_tools
import gateway
import model_router

load_dotenv()

logger = logging.getLogger("talos.ai")

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_TOOLS_DIR = os.path.join(_SCRIPT_DIR, "tools")
_tools_guide_cache: str | None = None

_MODELS = model_router.get_all_model_aliases()

_MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "5"))
_MAX_TOOL_CALLS_PER_ROUND = int(os.getenv("MAX_TOOL_CALLS_PER_ROUND", "20"))
_MAX_COMMAND_TIMEOUT = int(os.getenv("MAX_COMMAND_TIMEOUT", "120"))
_MAX_WORKFLOW_STEPS = int(os.getenv("MAX_WORKFLOW_STEPS", "12"))
_MAX_SUBAGENT_TOOL_ROUNDS = int(os.getenv("MAX_SUBAGENT_TOOL_ROUNDS", "5"))
_MAX_SUBAGENT_TOOL_CALLS_PER_ROUND = int(os.getenv("MAX_SUBAGENT_TOOL_CALLS_PER_ROUND", "15"))

SendFunc = Callable[[str], Awaitable[None]]


def reload_clients():
    global _tools_guide_cache
    global _MAX_TOOL_ROUNDS, _MAX_TOOL_CALLS_PER_ROUND
    global _MAX_COMMAND_TIMEOUT, _MAX_WORKFLOW_STEPS
    global _MAX_SUBAGENT_TOOL_ROUNDS, _MAX_SUBAGENT_TOOL_CALLS_PER_ROUND
    _tools_guide_cache = None
    model_router.reload_clients()
    load_dotenv(override=True)
    _MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "5"))
    _MAX_TOOL_CALLS_PER_ROUND = int(os.getenv("MAX_TOOL_CALLS_PER_ROUND", "20"))
    _MAX_COMMAND_TIMEOUT = int(os.getenv("MAX_COMMAND_TIMEOUT", "120"))
    _MAX_WORKFLOW_STEPS = int(os.getenv("MAX_WORKFLOW_STEPS", "12"))
    _MAX_SUBAGENT_TOOL_ROUNDS = int(os.getenv("MAX_SUBAGENT_TOOL_ROUNDS", "5"))
    _MAX_SUBAGENT_TOOL_CALLS_PER_ROUND = int(os.getenv("MAX_SUBAGENT_TOOL_CALLS_PER_ROUND", "15"))


def list_models() -> list[str]:
    return model_router.list_provider_models()


def _load_tools_guide() -> str:
    global _tools_guide_cache
    if _tools_guide_cache is not None:
        return _tools_guide_cache
    guides = []
    if os.path.isdir(_TOOLS_DIR):
        for filename in sorted(os.listdir(_TOOLS_DIR)):
            if filename.endswith(".md"):
                filepath = os.path.join(_TOOLS_DIR, filename)
                try:
                    with open(filepath, "r") as f:
                        content = f.read().strip()
                    tool_name = filename[:-3]
                    guides.append(f"### {tool_name}\n{content}")
                except Exception:
                    pass
    _tools_guide_cache = "\n\n".join(guides) if guides else ""
    return _tools_guide_cache


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _build_system(user_id: int, current_message: str = "", include_memories: bool = True) -> str | None:
    parts = []
    prompt = db.read_system_prompt()
    if prompt:
        parts.append(prompt)

    parts.append(f"[Current time] {_now_str()}")

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

    parts.append(f"[Current time] {_now_str()}")

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
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": (
                    "Read a file's contents with line numbers. Always read a file BEFORE editing it. "
                    "Returns encoding info and pagination metadata. On error, returns error_code, hint, and recoverable flag."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path (absolute or relative to project root)"},
                        "offset": {"type": "number", "description": "Line number to start from (1-indexed, default 0 = start)"},
                        "limit": {"type": "number", "description": "Max lines to return (default 500, max 2000)"}
                    },
                    "required": ["path"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": (
                    "Write content to a file atomically. Creates the file if it doesn't exist, overwrites if it does. "
                    "Returns a unified diff preview for existing files. Warns if content is identical. "
                    "Uses atomic writes (write to temp then move) to prevent corruption on crash."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path (absolute or relative to project root)"},
                        "content": {"type": "string", "description": "The full content to write"},
                        "create_dirs": {"type": "boolean", "description": "Create parent directories if they don't exist (default false)"}
                    },
                    "required": ["path", "content"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "edit_file",
                "description": (
                    "Find and replace an exact string in a file. Use this for targeted edits instead of rewriting "
                    "entire files. The old_string must match the file content EXACTLY (indentation, whitespace, etc). "
                    "On failure, returns a fuzzy match suggestion and file preview to help you self-correct. "
                    "Uses atomic writes to prevent corruption."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path (absolute or relative to project root)"},
                        "old_string": {"type": "string", "description": "The exact string to find in the file"},
                        "new_string": {"type": "string", "description": "The replacement string"},
                        "replace_all": {"type": "boolean", "description": "Replace all occurrences instead of just the first (default false)"}
                    },
                    "required": ["path", "old_string", "new_string"]
                }
            }
        }
    ]

    tools.append({
            "type": "function",
            "function": {
                "name": "create_project",
                "description": (
                    "Create a web project (website, presentation, app) and make it instantly live. "
                    "Pass the full HTML content and this tool handles everything: creates the directory, "
                    "writes index.html, registers it in the gateway, and returns the full public URL. "
                    "You MUST send the returned url to the user — it is the live clickable link."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Project name (alphanumeric, hyphens, underscores). Used in the URL."},
                        "html": {"type": "string", "description": "The full HTML content for index.html"},
                        "description": {"type": "string", "description": "Short description of the project"}
                    },
                    "required": ["name", "html"]
                }
            }
        })
    tools.append({
            "type": "function",
            "function": {
                "name": "list_projects",
                "description": "List all registered projects in the gateway with their URLs and status.",
                "parameters": {"type": "object", "properties": {}}
            }
        })

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

        elif tool_name == "read_file":
            path = str(tool_args.get("path", "")).strip()
            if not path:
                return json.dumps({"error": "No path provided"})
            offset = tool_args.get("offset", 0)
            limit = tool_args.get("limit", 500)
            if not isinstance(offset, (int, float)):
                offset = 0
            if not isinstance(limit, (int, float)):
                limit = 500
            limit = max(1, min(int(limit), 2000))
            result = file_tools.read_file(path, offset=int(offset), limit=int(limit))
            return json.dumps(result, indent=2)

        elif tool_name == "write_file":
            path = str(tool_args.get("path", "")).strip()
            content = tool_args.get("content", "")
            if not path:
                return json.dumps({"error": "No path provided"})
            if content is None:
                content = ""
            create_dirs = bool(tool_args.get("create_dirs", False))
            result = file_tools.write_file(path, str(content), create_dirs=create_dirs)
            return json.dumps(result, indent=2)

        elif tool_name == "edit_file":
            path = str(tool_args.get("path", "")).strip()
            old_string = tool_args.get("old_string", "")
            new_string = tool_args.get("new_string", "")
            replace_all = bool(tool_args.get("replace_all", False))
            if not path:
                return json.dumps({"error": "No path provided"})
            if old_string is None:
                old_string = ""
            if new_string is None:
                new_string = ""
            result = file_tools.edit_file(path, str(old_string), str(new_string), replace_all=replace_all)
            return json.dumps(result, indent=2)

        elif tool_name == "create_project":
            name = str(tool_args.get("name", "")).strip()
            html = str(tool_args.get("html", "")).strip()
            if not name:
                return json.dumps({"error": "No name provided"})
            if not html:
                return json.dumps({"error": "No html content provided"})
            description = str(tool_args.get("description", "")).strip()
            reg = gateway.register_project(name, description=description)
            index_path = os.path.join(reg["path"], "index.html")
            write_result = file_tools.write_file(index_path, html, create_dirs=True)
            if "error" in write_result:
                return json.dumps({"error": f"Failed to write index.html: {write_result['error']}"})
            return json.dumps({
                "status": "live",
                "url": reg["url"],
                "share_this_link": reg["url"],
                "path": reg["path"],
                "instruction": "Send the url to the user — this is the live clickable link to their project",
            }, indent=2)

        elif tool_name == "list_projects":
            return json.dumps({"projects": gateway.list_projects()}, indent=2)

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
            if send_func:
                await send_func(text, voice=True)
                return json.dumps({"sent": True})
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

    for _ in range(max_rounds):
        response = await model_router.call_model(model_id, messages, tools)
        
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
        response = await model_router.call_model(model_id, messages, None)
        return response.get("content", "I could not generate a response right now.")
    except Exception:
        return "I ran out of processing rounds. Please try again."


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
        summary = await model_router.call_model_simple(model_id, system_msg, f"Summarize this conversation:\n\n{conversation}")
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
        tools=_get_all_tools(include_subagent=True, include_telegram=send_func is not None),
        user_id=user_id,
        send_func=send_func,
        allow_subagent=True,
    )

    db.add_message(user_id, "user", text)
    db.add_message(user_id, "assistant", reply)
    await _maybe_summarize(user_id, model)
    return reply
