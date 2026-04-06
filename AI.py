import os
import sys
import asyncio
import json
import logging
import re
import subprocess
import time
import unicodedata
from datetime import datetime, timezone
from typing import Callable, Awaitable
from dotenv import load_dotenv
import db
import memory
import terminal_tools
import environment
import cron_jobs
import websearch
import scrapy_scraper
import file_tools
import spreadsheet_tools
import docx_tools
import dynamic_tools
import gateway
import model_router
import browser_automation
import google_integration
import email_tools
import activity_tracker

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
_MAX_ORCHESTRATOR_WALL_TIMEOUT_S = int(os.getenv("MAX_ORCHESTRATOR_WALL_TIMEOUT_S", "300"))
_MAX_SUBAGENT_TOOL_ROUNDS = int(os.getenv("MAX_SUBAGENT_TOOL_ROUNDS", "5"))
_MAX_SUBAGENT_TOOL_CALLS_PER_ROUND = int(os.getenv("MAX_SUBAGENT_TOOL_CALLS_PER_ROUND", "15"))
_MAX_SUBAGENT_WALL_TIMEOUT_S = int(os.getenv("MAX_SUBAGENT_WALL_TIMEOUT_S", "180"))
_SUBAGENT_MAX_TELEGRAM_MESSAGES = int(os.getenv("SUBAGENT_MAX_TELEGRAM_MESSAGES", "3"))
_SUBAGENT_MAX_TELEGRAM_MESSAGE_CHARS = int(os.getenv("SUBAGENT_MAX_TELEGRAM_MESSAGE_CHARS", "260"))
_SUBAGENT_MIN_UPDATE_INTERVAL_S = float(os.getenv("SUBAGENT_MIN_UPDATE_INTERVAL_S", "30"))

SendFunc = Callable[..., Awaitable[None]]


def reload_clients():
    global _tools_guide_cache
    global _MAX_TOOL_ROUNDS, _MAX_TOOL_CALLS_PER_ROUND
    global _MAX_COMMAND_TIMEOUT, _MAX_WORKFLOW_STEPS, _MAX_ORCHESTRATOR_WALL_TIMEOUT_S
    global _MAX_SUBAGENT_TOOL_ROUNDS, _MAX_SUBAGENT_TOOL_CALLS_PER_ROUND, _MAX_SUBAGENT_WALL_TIMEOUT_S
    global _SUBAGENT_MAX_TELEGRAM_MESSAGES, _SUBAGENT_MAX_TELEGRAM_MESSAGE_CHARS, _SUBAGENT_MIN_UPDATE_INTERVAL_S
    _tools_guide_cache = None
    model_router.reload_clients()
    load_dotenv(override=True)
    _MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "20"))
    _MAX_TOOL_CALLS_PER_ROUND = int(os.getenv("MAX_TOOL_CALLS_PER_ROUND", "20"))
    _MAX_COMMAND_TIMEOUT = int(os.getenv("MAX_COMMAND_TIMEOUT", "120"))
    _MAX_WORKFLOW_STEPS = int(os.getenv("MAX_WORKFLOW_STEPS", "12"))
    _MAX_ORCHESTRATOR_WALL_TIMEOUT_S = int(os.getenv("MAX_ORCHESTRATOR_WALL_TIMEOUT_S", "300"))
    _MAX_SUBAGENT_TOOL_ROUNDS = int(os.getenv("MAX_SUBAGENT_TOOL_ROUNDS", "5"))
    _MAX_SUBAGENT_TOOL_CALLS_PER_ROUND = int(os.getenv("MAX_SUBAGENT_TOOL_CALLS_PER_ROUND", "15"))
    _MAX_SUBAGENT_WALL_TIMEOUT_S = int(os.getenv("MAX_SUBAGENT_WALL_TIMEOUT_S", "180"))
    _SUBAGENT_MAX_TELEGRAM_MESSAGES = int(os.getenv("SUBAGENT_MAX_TELEGRAM_MESSAGES", "3"))
    _SUBAGENT_MAX_TELEGRAM_MESSAGE_CHARS = int(os.getenv("SUBAGENT_MAX_TELEGRAM_MESSAGE_CHARS", "260"))
    _SUBAGENT_MIN_UPDATE_INTERVAL_S = float(os.getenv("SUBAGENT_MIN_UPDATE_INTERVAL_S", "30"))


def list_models() -> list[str]:
    return model_router.list_models_with_provider()


def _load_tools_guide() -> str:
    global _tools_guide_cache
    if _tools_guide_cache is not None:
        return _tools_guide_cache
    lines = []
    if os.path.isdir(_TOOLS_DIR):
        for filename in sorted(os.listdir(_TOOLS_DIR)):
            if not filename.endswith(".md"):
                continue
            filepath = os.path.join(_TOOLS_DIR, filename)
            try:
                with open(filepath, "r") as f:
                    first_lines = [f.readline() for _ in range(5)]
                title = first_lines[0].strip().lstrip("# ").strip()
                desc = ""
                for line in first_lines[1:]:
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#"):
                        desc = stripped
                        break
                tool_name = filename[:-3]
                lines.append(f"- {tool_name}: {desc}" if desc else f"- {tool_name}")
            except Exception:
                pass
    summary = "You have access to the following tools. Use `read_tool_guide` to read the full usage guide for any tool before using it.\n\n" + "\n".join(lines) if lines else ""
    _tools_guide_cache = summary
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

    email_configured = os.getenv("HIMALAYA_CONFIG", "").strip()
    if email_configured and os.path.isfile(email_configured):
        parts.append("[Email] Email is configured and available. Use the email_execute tool for all email operations (send, read, reply, list). Do NOT tell the user email is not configured — it is.")

    env_context = environment.get_environment_context()
    if env_context:
        parts.append(f"[Environment]\n{env_context}")
    tg_format = environment.get_telegram_formatting_guide()
    if tg_format:
        parts.append(tg_format)

    parts.append(
        "[Orchestrator Instructions]\n"
        "You are the orchestrator. When a task benefits from decomposition, use spawn_subagent "
        "to delegate focused subtasks. Subagent Telegram updates must be minimal and concise. "
        "Use at most one optional progress update plus one completion update per subagent, "
        "and never stream step-by-step internal logs. "
        "For action requests (open/click/search/screenshot/send), execute tools first and then report results. "
        "Do not return a plan-only final reply like 'let me' or 'I will'. "
        "For browser tasks, stay in the current tab unless the user explicitly asks for new tabs/windows. "
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
        "IMPORTANT: You have access to send_telegram_message for concise status updates only.\n"
        "Rules:\n"
        "1. Send at most one brief start update and one completion update\n"
        "2. Send one extra progress update only for a milestone, blocker, key decision, or long silence\n"
        "3. Never stream per-step logs, repeated narration, raw transcripts, or generic 'still working' spam\n"
        "4. Keep each update short (one paragraph, max ~240 chars)\n"
        "Write updates in clean plain English with complete sentences. Avoid random or unusual characters.\n"
        "Do not run passive wait commands like sleep/timeout/checking-in loops.\n"
        "Never end on a 'still working' update without later sending an explicit completion or failure update.\n"
        "Keep messages concise and useful. Sign off with your role in brackets so the user "
        "knows which subagent is talking, e.g. [researcher] or [executor]."
    )
    parts.append(f"[Subagent role]\n{role or 'general'}")
    parts.append(f"[Delegated task]\n{task}")
    if context.strip():
        parts.append(f"[Delegation context]\n{context.strip()}")

    email_configured = os.getenv("HIMALAYA_CONFIG", "").strip()
    if email_configured and os.path.isfile(email_configured):
        parts.append("[Email] Email is configured and available. Use the email_execute tool for all email operations.")

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


def _is_passive_wait_command(command: str) -> bool:
    normalized = " ".join((command or "").strip().lower().split())
    if not normalized:
        return False

    patterns = [
        r"^sleep\s+\d+(\.\d+)?(\s*([;&|]{1,2})\s*(echo|printf)\b.*)?$",
        r"^timeout(\.exe)?\s+/t\s+\d+(\s+/nobreak)?(\s*([;&|]{1,2})\s*echo\b.*)?$",
        r"^ping\s+-n\s+\d+\s+127\.0\.0\.1(\s*>\s*nul)?$",
        r"^(powershell(\.exe)?\s+.+\s+)?start-sleep\b.*$",
    ]
    return any(re.match(pat, normalized) for pat in patterns)


def _looks_like_completion_message(text: str) -> bool:
    lowered = text.lower()
    markers = (
        "done",
        "complete",
        "completed",
        "finished",
        "result",
        "summary",
        "success",
        "failed",
        "failure",
        "error",
    )
    return any(marker in lowered for marker in markers)


def _compact_telegram_update_text(message: str, max_chars: int) -> str:
    normalized = unicodedata.normalize("NFKC", message or "")
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"^\[[0-9]{4}-[0-9]{2}-[0-9]{2}[^\]]*\]\s*", "", normalized.strip())
    lines = [line.strip(" \t-") for line in normalized.split("\n") if line.strip()]
    flattened = re.sub(r"\s+", " ", " ".join(lines)).strip()
    if not flattened:
        return ""

    sentences = re.split(r"(?<=[.!?])\s+", flattened)
    deduped: list[str] = []
    seen: set[str] = set()
    for sentence in sentences:
        key = sentence.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(sentence)

    compact = " ".join(deduped).strip()
    if len(compact) > max_chars:
        compact = compact[: max_chars - 3].rstrip() + "..."
    return compact


async def _send_via_send_func(send_func: SendFunc, message: str, voice: bool = False) -> None:
    try:
        if voice:
            await send_func(message, voice=True)
        else:
            await send_func(message)
    except TypeError:
        await send_func(message)


async def _send_document_via_send_func(send_func: SendFunc, path: str, caption: str = "") -> None:
    note = str(caption or "").strip()
    try:
        await send_func(note, document_path=path, caption=note)
    except TypeError:
        fallback = note if note else "File sent."
        await _send_via_send_func(send_func, f"{fallback} File path: {path}")


async def _send_photo_via_send_func(send_func: SendFunc, path: str, caption: str = "") -> None:
    note = str(caption or "").strip()
    try:
        await send_func(note, photo_path=path, caption=note)
    except TypeError:
        fallback = note if note else "Screenshot captured."
        await _send_via_send_func(send_func, f"{fallback} Image path: {path}")


def _default_image_artifact_path(prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(_SCRIPT_DIR, "logs", "browser")
    os.makedirs(out_dir, exist_ok=True)
    return os.path.join(out_dir, f"{prefix}_{stamp}.png")


def _resolve_output_image_path(path: str | None, prefix: str) -> str:
    raw = str(path or "").strip()
    if raw:
        resolved = raw if os.path.isabs(raw) else os.path.join(_SCRIPT_DIR, raw)
        resolved = os.path.realpath(resolved)
        if not resolved.startswith(os.path.realpath(_SCRIPT_DIR) + os.sep) and resolved != os.path.realpath(_SCRIPT_DIR):
            return _default_image_artifact_path(prefix)
        parent = os.path.dirname(resolved)
        if parent:
            os.makedirs(parent, exist_ok=True)
        return resolved
    return _default_image_artifact_path(prefix)


def _resolve_existing_image_path(path: str) -> str:
    raw = str(path or "").strip()
    if not raw:
        return ""
    resolved = raw if os.path.isabs(raw) else os.path.join(_SCRIPT_DIR, raw)
    resolved = os.path.realpath(resolved)
    if not resolved.startswith(os.path.realpath(_SCRIPT_DIR) + os.sep) and resolved != os.path.realpath(_SCRIPT_DIR):
        return ""
    return resolved if os.path.isfile(resolved) else ""


def _capture_screen_screenshot(path: str | None = None) -> dict:
    out_path = _resolve_output_image_path(path, "screen")

    try:
        if sys.platform == "darwin":
            result = subprocess.run(
                ["screencapture", "-x", out_path],
                capture_output=True,
                text=True,
                timeout=20,
            )
        elif sys.platform.startswith("linux"):
            result = None
            commands = [
                ["gnome-screenshot", "-f", out_path],
                ["grim", out_path],
                ["import", "-window", "root", out_path],
            ]
            for cmd in commands:
                try:
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
                except FileNotFoundError:
                    continue
                if result.returncode == 0:
                    break
            if result is None or result.returncode != 0:
                return {
                    "error": "No supported Linux screenshot command found",
                    "hint": "Install gnome-screenshot or grim, then try again.",
                }
        elif sys.platform == "win32":
            ps_script = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "Add-Type -AssemblyName System.Drawing; "
                "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds; "
                "$bmp=New-Object System.Drawing.Bitmap($b.Width,$b.Height); "
                "$g=[System.Drawing.Graphics]::FromImage($bmp); "
                "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size); "
                f"$bmp.Save('{out_path}', [System.Drawing.Imaging.ImageFormat]::Png); "
                "$g.Dispose(); $bmp.Dispose();"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True,
                text=True,
                timeout=30,
            )
        else:
            return {"error": f"Screen capture is not supported on this platform: {sys.platform}"}

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            return {"error": "Screen capture command failed", "detail": detail}

        if not os.path.isfile(out_path):
            return {"error": "Screen capture did not produce an image file"}

        return {"ok": True, "path": out_path}
    except Exception as e:
        return {"error": f"Screen capture failed: {e}"}


async def _capture_browser_screenshot(path: str | None = None, full_page: bool = False, page_index: int | None = None) -> dict:
    out_path = _resolve_output_image_path(path, "browser")

    result = await browser_automation.run_steps(
        steps=[{"action": "screenshot", "path": out_path, "full_page": bool(full_page)}],
        stop_on_error=True,
        page_index=page_index,
        auto_connect=True,
    )

    if not result.get("ok"):
        return {"error": "Browser screenshot failed", "detail": result}

    step_results = result.get("results") or []
    screenshot_path = out_path
    if step_results and isinstance(step_results[0], dict):
        screenshot_path = step_results[0].get("result", {}).get("path", out_path)

    if not os.path.isfile(screenshot_path):
        return {"error": "Browser screenshot file was not created", "detail": result}

    return {"ok": True, "path": screenshot_path}


def _build_subagent_send_func(send_func: SendFunc | None) -> SendFunc | None:
    if send_func is None:
        return None

    max_messages = max(1, min(int(_SUBAGENT_MAX_TELEGRAM_MESSAGES), 6))
    max_chars = max(80, min(int(_SUBAGENT_MAX_TELEGRAM_MESSAGE_CHARS), 1000))
    min_interval = max(0.0, min(float(_SUBAGENT_MIN_UPDATE_INTERVAL_S), 120.0))

    sent_count = 0
    last_sent_at = 0.0
    completion_sent = False

    async def _wrapped(message: str = "", voice: bool = False, photo_path: str | None = None, document_path: str | None = None, caption: str = "") -> None:
        nonlocal sent_count, last_sent_at, completion_sent

        if document_path:
            await _send_document_via_send_func(send_func, str(document_path), str(caption or message or ""))
            return

        if photo_path:
            remaining_slots = max_messages - sent_count
            if remaining_slots <= 0:
                return

            now = asyncio.get_running_loop().time()
            if sent_count > 0 and (now - last_sent_at) < min_interval:
                return

            compact_caption = _compact_telegram_update_text(str(caption or message or ""), max_chars)
            await _send_photo_via_send_func(send_func, str(photo_path), compact_caption)
            sent_count += 1
            last_sent_at = now
            if compact_caption and _looks_like_completion_message(compact_caption):
                completion_sent = True
            return

        compact = _compact_telegram_update_text(str(message), max_chars)
        if not compact:
            return

        is_completion = _looks_like_completion_message(compact)
        if completion_sent:
            return

        remaining_slots = max_messages - sent_count
        if remaining_slots <= 0:
            return

        now = asyncio.get_running_loop().time()
        if not is_completion:
            if sent_count > 0 and (now - last_sent_at) < min_interval:
                return
            # Keep one slot for an explicit completion/failure update.
            if remaining_slots == 1:
                return

        await _send_via_send_func(send_func, compact, voice=voice)
        sent_count += 1
        last_sent_at = now
        if is_completion:
            completion_sent = True

    return _wrapped


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

def _get_all_tools(include_subagent: bool = True, include_telegram: bool = False):
    tools = [
        {
            "type": "function",
            "function": {
                "name": "execute_command",
                "description": "Execute a terminal command. The environment (native/docker/firejail) is configured by the user. Check the [Environment] section in your context to understand your access level. Do NOT use this for email — use email_execute instead.",
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
                        "description": {"type": "string", "description": "Optional context or annotation about why this was saved (e.g., 'The user wanted me to remember this as a test word')"},
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
                "name": "set_model_prefs",
                "description": "Set the main and image models for this user.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "main_model": {"type": "string", "description": "Main model for text tasks"},
                        "image_model": {"type": "string", "description": "Model for image understanding"}
                    }
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
                "name": "google_execute",
                "description": (
                    "Execute a Google ecosystem action. Works directly via Google APIs (no Apps Script needed). "
                    "Supports: calendar.list_events, calendar.create_event, calendar.list_calendars, "
                    "drive.list_files, drive.get_file, drive.export_file, sheets.get_values, sheets.append_row. "
                    "If GOOGLE_APPS_SCRIPT_URL is configured, any custom action is forwarded to the Apps Script."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "Action name, e.g. 'calendar.list_events', 'calendar.create_event', 'drive.list_files', 'sheets.get_values'"},
                        "payload": {"type": "object", "description": "Action payload object"}
                    },
                    "required": ["action"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "email_execute",
                "description": (
                    "Execute an email action via the Himalaya CLI. "
                    "ALWAYS use this tool for ALL email operations — never use raw shell commands (cat, himalaya, etc.) to interact with email. "
                    "This tool handles config paths, accounts, and error formatting automatically."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": (
                                "Email action to run: list_accounts, list_folders, list_messages, read_message, "
                                "thread_message, send_message, reply_message, forward_message, move_messages, "
                                "copy_messages, delete_messages"
                            ),
                        },
                        "account": {"type": "string", "description": "Optional Himalaya account name"},
                        "folder": {"type": "string", "description": "Optional source folder (defaults to inbox)"},
                        "page": {"type": "number", "description": "Optional page number for list_messages"},
                        "message_id": {"type": "number", "description": "Single message/envelope id for read/reply/forward/thread"},
                        "message_ids": {
                            "type": "array",
                            "items": {"type": "number"},
                            "description": "List of ids for bulk actions (move/copy/delete)",
                        },
                        "target_folder": {"type": "string", "description": "Target folder for move/copy"},
                        "preview": {"type": "boolean", "description": "Read/thread in preview mode (avoid marking seen). Default true."},
                        "reply_all": {"type": "boolean", "description": "For reply_message: include all recipients"},
                        "to": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Recipients for send_message",
                        },
                        "cc": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "CC recipients for send_message",
                        },
                        "bcc": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "BCC recipients for send_message",
                        },
                        "subject": {"type": "string", "description": "Subject for send_message"},
                        "body": {"type": "string", "description": "Body for send/reply/forward"},
                        "headers": {
                            "type": "object",
                            "description": "Optional custom headers as key-value pairs",
                            "additionalProperties": {"type": "string"},
                        },
                    },
                    "required": ["action"],
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "browser_start_chrome_debug",
                "description": "Start Google Chrome with remote debugging enabled so TALOS can attach to your existing logged-in browser state.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "endpoint": {"type": "string", "description": "CDP endpoint (default: http://127.0.0.1:9222)"},
                        "port": {"type": "number", "description": "Debug port (default: 9222)"},
                        "chrome_path": {"type": "string", "description": "Optional explicit Chrome executable path"},
                        "user_data_dir": {"type": "string", "description": "Chrome user data directory for session reuse"},
                        "profile_directory": {"type": "string", "description": "Chrome profile directory name, e.g. 'Default'"},
                        "connect": {"type": "boolean", "description": "Auto-connect after launch (default: true)"},
                        "startup_timeout_s": {"type": "number", "description": "Wait time for Chrome startup (default: 20s)"}
                    }
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "browser_connect",
                "description": "Attach to a Chrome browser via CDP, ideally your already logged-in Chrome profile.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "endpoint": {"type": "string", "description": "CDP endpoint (default: http://127.0.0.1:9222)"},
                        "start_if_needed": {"type": "boolean", "description": "Try launching Chrome debug mode if endpoint is unavailable (default: true)"},
                        "allow_isolated_fallback": {"type": "boolean", "description": "Allow isolated profile fallback if existing-profile startup fails (default: false)"},
                        "port": {"type": "number", "description": "Debug port when start_if_needed=true"},
                        "chrome_path": {"type": "string", "description": "Optional explicit Chrome executable path"},
                        "user_data_dir": {"type": "string", "description": "Chrome user data directory for session reuse"},
                        "profile_directory": {"type": "string", "description": "Chrome profile directory name, e.g. 'Default'"},
                        "startup_timeout_s": {"type": "number", "description": "Wait time for Chrome startup (default: 20s)"}
                    }
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "browser_run",
                "description": "Run deterministic browser automation steps on the connected Chrome session.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "steps": {
                            "type": "array",
                            "description": "Ordered automation steps.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "action": {
                                        "type": "string",
                                        "description": "Action: goto, click, type, press, wait_for, extract, select, scroll, screenshot, assert, set_page, new_tab, close_tab"
                                    },
                                    "url": {"type": "string"},
                                    "selector": {"type": "string"},
                                    "text": {"type": "string"},
                                    "key": {"type": "string"},
                                    "attr": {"type": "string"},
                                    "all": {"type": "boolean"},
                                    "limit": {"type": "number"},
                                    "timeout_ms": {"type": "number"},
                                    "wait_until": {"type": "string"},
                                    "state": {"type": "string"},
                                    "milliseconds": {"type": "number"},
                                    "url_contains": {"type": "string"},
                                    "button": {"type": "string"},
                                    "click_count": {"type": "number"},
                                    "clear": {"type": "boolean"},
                                    "delay_ms": {"type": "number"},
                                    "value": {"type": "string"},
                                    "label": {"type": "string"},
                                    "index": {"type": "number"},
                                    "x": {"type": "number"},
                                    "y": {"type": "number"},
                                    "path": {"type": "string"},
                                    "full_page": {"type": "boolean"},
                                    "page_index": {"type": "number"}
                                },
                                "required": ["action"]
                            }
                        },
                        "stop_on_error": {"type": "boolean", "description": "Stop at first failing step (default: true)"},
                        "auto_connect": {"type": "boolean", "description": "Auto-connect/start browser when no session exists (default: true)"},
                        "default_timeout_ms": {"type": "number", "description": "Default step timeout in ms (default: 15000)"},
                        "page_index": {"type": "number", "description": "Optional page index to use as active tab"}
                    },
                    "required": ["steps"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "browser_state",
                "description": "Get the current browser automation session state, active URL, and open tabs.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "include_tabs": {"type": "boolean", "description": "Include tab list (default: true)"},
                        "include_page_text": {"type": "boolean", "description": "Include visible body text snippet"},
                        "text_limit": {"type": "number", "description": "Maximum body text characters (default: 1200)"}
                    }
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "browser_disconnect",
                "description": "Disconnect from the active browser automation session.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "terminate_launched": {"type": "boolean", "description": "Also terminate Chrome launched by browser_start_chrome_debug"}
                    }
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
                    "Write the FULL content to a file atomically. ONLY use this to CREATE new files. "
                    "For editing existing files, use edit_file instead — it is safer and more efficient. "
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
                    "PREFERRED tool for modifying existing files. Find and replace an exact string in a file. "
                    "Always use this instead of write_file when editing — it is safer, faster, and shows exactly what changed. "
                    "The old_string must match the file content EXACTLY (indentation, whitespace, etc). "
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
        },
        {
            "type": "function",
            "function": {
                "name": "spreadsheet_execute",
                "description": (
                    "Execute advanced XLSX actions. "
                    "Use pandas for full workbook reads, openpyxl for edits that preserve formulas/formatting, "
                    "LibreOffice recalc via scripts/recalc.py, formula-error verification, and financial color coding."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": (
                                "Spreadsheet action: read_with_pandas, edit_with_openpyxl, "
                                "recalculate_with_libreoffice, verify_formula_errors, apply_financial_color_coding"
                            )
                        },
                        "path": {"type": "string", "description": "Workbook path (.xlsx)"},
                        "sheet_name": {"type": "string", "description": "Optional sheet name"},
                        "operations": {
                            "type": "array",
                            "description": "For edit_with_openpyxl: list of operations",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "action": {"type": "string"},
                                    "sheet": {"type": "string"},
                                    "cell": {"type": "string"},
                                    "value": {"description": "Cell value (string/number/boolean/null)"},
                                    "formula": {"type": "boolean"},
                                    "number_format": {"type": "string"},
                                    "values": {"type": "array", "description": "Row values (mixed types allowed)"}
                                }
                            }
                        },
                        "create_if_missing": {"type": "boolean", "description": "Create workbook if missing (edit_with_openpyxl only)"},
                        "max_rows": {"type": "number", "description": "Preview rows for read_with_pandas"},
                        "max_cols": {"type": "number", "description": "Preview columns for read_with_pandas"},
                        "output_path": {"type": "string", "description": "Optional output workbook path for recalc"},
                        "timeout_s": {"type": "number", "description": "Recalc timeout seconds"},
                        "script_path": {"type": "string", "description": "Optional custom recalc script path"},
                        "max_errors": {"type": "number", "description": "Max errors to report for verify_formula_errors"},
                        "input_ranges": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional input ranges for financial color coding (e.g. B3:D50)"
                        },
                        "header_rows": {"type": "number", "description": "Header rows to skip before auto input coloring"}
                    },
                    "required": ["action", "path"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "docx_execute",
                "description": (
                    "Execute advanced DOCX actions. "
                    "Create docs via JavaScript docx library, edit existing docs by XML unpack/edit/repack, "
                    "apply tracked changes tags, enforce DXA sizing, normalize smart-quote entities, and validate XML."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": (
                                "DOCX action: create_with_docx_js, edit_xml, track_replace, set_page_size_dxa, "
                                "set_table_widths_dxa, normalize_text, validate_xml"
                            )
                        },
                        "path": {"type": "string", "description": "DOCX path"},
                        "title": {"type": "string", "description": "Title for create_with_docx_js"},
                        "paragraphs": {"type": "array", "items": {"type": "string"}, "description": "Paragraphs for create_with_docx_js"},
                        "table_rows": {
                            "type": "array",
                            "description": "Optional rows for one generated table",
                            "items": {"type": "array", "items": {"type": "string"}
                            }
                        },
                        "page_width_dxa": {"type": "number", "description": "Page width in DXA (twips)"},
                        "page_height_dxa": {"type": "number", "description": "Page height in DXA (twips)"},
                        "edits": {
                            "type": "array",
                            "description": "For edit_xml: list of XML edits",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "xml_path": {"type": "string"},
                                    "find": {"type": "string"},
                                    "replace": {"type": "string"},
                                    "replace_all": {"type": "boolean"}
                                }
                            }
                        },
                        "validate_after": {"type": "boolean", "description": "Validate XML after edit_xml"},
                        "old_text": {"type": "string", "description": "For track_replace: text to mark as deleted"},
                        "new_text": {"type": "string", "description": "For track_replace: text to mark as inserted"},
                        "author": {"type": "string", "description": "Tracked change author"},
                        "width_dxa": {"type": "number", "description": "DXA width for set_page_size_dxa/set_table_widths_dxa"},
                        "height_dxa": {"type": "number", "description": "DXA height for set_page_size_dxa"},
                        "no_unicode_bullets": {"type": "boolean", "description": "Normalize unicode bullets to hyphen"},
                        "smart_quotes_entities": {"type": "boolean", "description": "Normalize quotes/apostrophes to XML entities"},
                        "xml_paths": {"type": "array", "items": {"type": "string"}, "description": "Optional XML paths for normalize_text"}
                    },
                    "required": ["action", "path"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "create_tool",
                "description": (
                    "Create or update a dynamic tool that executes a command template with named arguments. "
                    "Use this when the user asks you to add a reusable tool."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Tool name (lowercase letters/numbers/underscores)"},
                        "description": {"type": "string", "description": "What this tool does"},
                        "command_template": {
                            "type": "string",
                            "description": "Shell command template. Use placeholders like {topic} for arguments."
                        },
                        "parameters": {
                            "type": "object",
                            "description": (
                                "Argument definitions map. Example: {\"topic\": {\"type\": \"string\", \"description\": \"Search query\"}, "
                                "\"limit\": {\"type\": \"integer\", \"description\": \"Max results\"}}"
                            )
                        },
                        "required": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Required argument names"
                        },
                        "timeout": {"type": "number", "description": "Default timeout in seconds (1-600)"},
                        "guide": {"type": "string", "description": "Optional extra usage notes for the generated tool doc"},
                        "overwrite": {"type": "boolean", "description": "Set true to replace an existing dynamic tool with the same name"}
                    },
                    "required": ["name", "description", "command_template"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "list_dynamic_tools",
                "description": "List all dynamic tools that were created with create_tool.",
                "parameters": {
                    "type": "object",
                    "properties": {}
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "delete_tool",
                "description": "Delete a previously created dynamic tool by name.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Dynamic tool name to delete"}
                    },
                    "required": ["name"]
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
                    "You MUST send the returned url to the user — it is the live clickable link. "
                    "For existing files on disk, use migrate_project instead — it copies the file directly "
                    "without needing to read its content first."
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
                "name": "migrate_project",
                "description": (
                    "Copy an existing HTML file on disk into the project gateway and make it live. "
                    "Use this when you already have an HTML file and want to serve it — no need to "
                    "read the file first. Just pass the source path and a project name. "
                    "Returns the full public URL. You MUST send the returned url to the user."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Project name (alphanumeric, hyphens, underscores). Used in the URL."},
                        "source_path": {"type": "string", "description": "Path to the existing HTML file to copy into the project"},
                        "description": {"type": "string", "description": "Short description of the project"}
                    },
                    "required": ["name", "source_path"]
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
                    "progress updates, and conclusions. Keep it short and avoid step-by-step narration."
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
        tools.append({
            "type": "function",
            "function": {
                "name": "send_telegram_photo",
                "description": (
                    "Send an existing image file to the user on Telegram. "
                    "Use this to share screenshots or visual results."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Image file path (absolute or relative to project root)"},
                        "caption": {"type": "string", "description": "Optional caption"}
                    },
                    "required": ["path"]
                }
            }
        })
        tools.append({
            "type": "function",
            "function": {
                "name": "send_telegram_document",
                "description": (
                    "Send any file (XLSX, PDF, DOCX, CSV, etc.) as a document attachment to the user on Telegram. "
                    "Use this for non-image files like spreadsheets, documents, and archives. "
                    "Do NOT use send_telegram_photo for non-image files — it will fail."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path (absolute or relative to project root)"},
                        "caption": {"type": "string", "description": "Optional caption describing the file"}
                    },
                    "required": ["path"]
                }
            }
        })
        tools.append({
            "type": "function",
            "function": {
                "name": "send_telegram_screenshot",
                "description": (
                    "Capture a screenshot from either the browser session or the desktop screen "
                    "and send it to the user on Telegram."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "enum": ["browser", "screen"], "description": "Screenshot source (default: browser)"},
                        "caption": {"type": "string", "description": "Optional caption"},
                        "path": {"type": "string", "description": "Optional output path for saved image"},
                        "full_page": {"type": "boolean", "description": "Browser only: capture full page (default false)"},
                        "page_index": {"type": "number", "description": "Browser only: tab index to capture"}
                    }
                }
            }
        })

    tools.append({
        "type": "function",
        "function": {
            "name": "read_tool_guide",
            "description": "Read the full usage guide for a tool. Use this BEFORE using a tool you're unfamiliar with.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_name": {"type": "string", "description": "The tool name (e.g. 'browser', 'file_tools', 'email')"}
                },
                "required": ["tool_name"]
            }
        }
    })

    tools.extend(dynamic_tools.get_tool_definitions())

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
    _agent_id: str = "orchestrator",
    _parent_agent_id: str | None = None,
) -> str:
    global _tools_guide_cache
    _t = activity_tracker.get_tracker()
    _tool_t0 = time.monotonic()

    def _with_activity_meta(extra: dict | None = None) -> dict:
        payload = dict(extra or {})
        if _parent_agent_id:
            payload.setdefault("parent_agent", _parent_agent_id)
        return payload

    try:
        if tool_name == "execute_command":
            command = str(tool_args.get("command", "")).strip()
            await _t.emit("command", _agent_id, "Command", f"Running: {command[:120]}", _with_activity_meta({"command": command}))
        elif tool_name == "spawn_subagent":
            pass
        else:
            await _t.emit("tool", _agent_id, "Tool call", f"Calling {tool_name}", _with_activity_meta({"tool": tool_name, "tool_args": {k: str(v)[:100] for k, v in tool_args.items() if isinstance(v, (str, int, float, bool))}}))

        if tool_name == "execute_command":
            command = str(tool_args.get("command", "")).strip()
            _tool_detail = f"Running: {command[:120]}"
            await _t.emit("command", _agent_id, "Command", _tool_detail, _with_activity_meta({"command": command}))
        elif tool_name == "spawn_subagent":
            pass
        else:
            _tool_detail = f"Calling {tool_name}"
            await _t.emit("tool", _agent_id, "Tool call", _tool_detail, _with_activity_meta({"tool": tool_name, "tool_args": {k: str(v)[:100] for k, v in tool_args.items() if isinstance(v, (str, int, float, bool))}}))

        if tool_name == "execute_command":
            timeout = tool_args.get("timeout", 30)
            if not isinstance(timeout, (int, float)):
                timeout = 30
            timeout = max(1, min(int(timeout), _MAX_COMMAND_TIMEOUT))
            if not command:
                return json.dumps({"error": "No command provided"})
            if _is_passive_wait_command(command):
                return json.dumps(
                    {
                        "error": "Passive wait commands are blocked",
                        "hint": "Do real work and report progress via send_telegram_message instead of sleep/timeout loops.",
                    },
                    indent=2,
                )
            result = await terminal_tools.execute_command(command, timeout=timeout)
            return json.dumps(result, indent=2)

        elif tool_name == "execute_workflow":
            steps = tool_args.get("steps")
            if not isinstance(steps, list) or not steps:
                return json.dumps({"error": "No steps provided"})
            cleaned = []
            for idx, step in enumerate(steps[:_MAX_WORKFLOW_STEPS], start=1):
                if not isinstance(step, dict):
                    continue
                cmd = str(step.get("command", "")).strip()
                if not cmd:
                    continue
                if _is_passive_wait_command(cmd):
                    return json.dumps(
                        {
                            "error": "Passive wait commands are blocked in workflows",
                            "step": idx,
                            "command": cmd,
                            "hint": "Remove sleep/timeout loops and continue with deterministic commands.",
                        },
                        indent=2,
                    )
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
            result = memory.save_memory(user_id, content, tool_args.get("category"), tool_args.get("importance", 5), tool_args.get("description"))
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

        elif tool_name == "set_model_prefs":
            main_model = str(tool_args.get("main_model", "")).strip()
            image_model = str(tool_args.get("image_model", "")).strip()
            available = set(model_router.list_provider_models())
            image_available = set(model_router.list_image_models())
            if main_model:
                bare = main_model.split("/", 1)[-1] if "/" in main_model else main_model
                if bare not in available and main_model not in available:
                    return json.dumps({"error": f"Unknown main model: {main_model}"}, indent=2)
                db.set_model(user_id, main_model)
            if image_model:
                bare = image_model.split("/", 1)[-1] if "/" in image_model else image_model
                if bare not in image_available and image_model not in image_available:
                    return json.dumps({"error": f"Unknown image model: {image_model}"}, indent=2)
                db.set_image_model(user_id, image_model)
            return json.dumps(
                {
                    "ok": True,
                    "main_model": db.get_model(user_id),
                    "image_model": db.get_image_model(user_id),
                },
                indent=2,
            )

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
            result = scrapy_scraper.scrape_url(
                url,
                formats=formats,
                only_main_content=only_main_content,
                timeout=timeout,
                max_age=max_age,
            )
            return json.dumps(result, indent=2)

        elif tool_name == "spreadsheet_execute":
            action = str(tool_args.get("action", "")).strip()
            if not action:
                return json.dumps({"error": "action is required"}, indent=2)
            result = spreadsheet_tools.execute(
                action=action,
                path=tool_args.get("path"),
                sheet_name=tool_args.get("sheet_name"),
                operations=tool_args.get("operations"),
                create_if_missing=tool_args.get("create_if_missing"),
                max_rows=tool_args.get("max_rows"),
                max_cols=tool_args.get("max_cols"),
                output_path=tool_args.get("output_path"),
                timeout_s=tool_args.get("timeout_s"),
                script_path=tool_args.get("script_path"),
                max_errors=tool_args.get("max_errors"),
                input_ranges=tool_args.get("input_ranges"),
                header_rows=tool_args.get("header_rows"),
            )
            return json.dumps(result, indent=2)

        elif tool_name == "docx_execute":
            action = str(tool_args.get("action", "")).strip()
            if not action:
                return json.dumps({"error": "action is required"}, indent=2)
            result = docx_tools.execute(
                action=action,
                path=tool_args.get("path"),
                title=tool_args.get("title"),
                paragraphs=tool_args.get("paragraphs"),
                table_rows=tool_args.get("table_rows"),
                page_width_dxa=tool_args.get("page_width_dxa"),
                page_height_dxa=tool_args.get("page_height_dxa"),
                edits=tool_args.get("edits"),
                validate_after=tool_args.get("validate_after"),
                old_text=tool_args.get("old_text"),
                new_text=tool_args.get("new_text"),
                author=tool_args.get("author"),
                width_dxa=tool_args.get("width_dxa"),
                height_dxa=tool_args.get("height_dxa"),
                no_unicode_bullets=tool_args.get("no_unicode_bullets"),
                smart_quotes_entities=tool_args.get("smart_quotes_entities"),
                xml_paths=tool_args.get("xml_paths"),
            )
            return json.dumps(result, indent=2)

        elif tool_name == "google_execute":
            action = str(tool_args.get("action", "")).strip()
            payload = tool_args.get("payload", {})
            if not action:
                return json.dumps({"error": "action is required"}, indent=2)
            if payload is None:
                payload = {}
            if not isinstance(payload, dict):
                return json.dumps({"error": "payload must be an object"}, indent=2)

            result = await google_integration.execute_apps_script(action=action, payload=payload)
            return json.dumps(result, indent=2)

        elif tool_name == "email_execute":
            action = str(tool_args.get("action", "")).strip()
            if not action:
                return json.dumps({"error": "action is required"}, indent=2)

            result = await email_tools.execute(
                action=action,
                account=tool_args.get("account"),
                folder=tool_args.get("folder"),
                page=tool_args.get("page"),
                message_id=tool_args.get("message_id"),
                message_ids=tool_args.get("message_ids"),
                target_folder=tool_args.get("target_folder"),
                preview=tool_args.get("preview"),
                reply_all=tool_args.get("reply_all"),
                to=tool_args.get("to"),
                cc=tool_args.get("cc"),
                bcc=tool_args.get("bcc"),
                subject=tool_args.get("subject"),
                body=tool_args.get("body"),
                headers=tool_args.get("headers"),
            )
            return json.dumps(result, indent=2)

        elif tool_name == "browser_start_chrome_debug":
            result = await browser_automation.start_chrome_debug(
                endpoint=tool_args.get("endpoint", "http://127.0.0.1:9222"),
                port=tool_args.get("port", 9222),
                chrome_path=tool_args.get("chrome_path", ""),
                user_data_dir=tool_args.get("user_data_dir", ""),
                profile_directory=tool_args.get("profile_directory", ""),
                connect=tool_args.get("connect", True),
                startup_timeout_s=tool_args.get("startup_timeout_s", 20),
            )
            return json.dumps(result, indent=2)

        elif tool_name == "browser_connect":
            result = await browser_automation.connect_browser(
                endpoint=tool_args.get("endpoint", "http://127.0.0.1:9222"),
                start_if_needed=tool_args.get("start_if_needed", True),
                allow_isolated_fallback=tool_args.get("allow_isolated_fallback", False),
                port=tool_args.get("port", 9222),
                chrome_path=tool_args.get("chrome_path", ""),
                user_data_dir=tool_args.get("user_data_dir", ""),
                profile_directory=tool_args.get("profile_directory", ""),
                startup_timeout_s=tool_args.get("startup_timeout_s", 20),
            )
            return json.dumps(result, indent=2)

        elif tool_name == "browser_run":
            steps = tool_args.get("steps")
            stop_on_error = tool_args.get("stop_on_error", True)
            default_timeout_ms = tool_args.get("default_timeout_ms", 15000)
            page_index = tool_args.get("page_index")
            result = await browser_automation.run_steps(
                steps=steps,
                stop_on_error=stop_on_error,
                default_timeout_ms=default_timeout_ms,
                page_index=page_index,
                auto_connect=tool_args.get("auto_connect"),
            )
            return json.dumps(result, indent=2)

        elif tool_name == "browser_state":
            include_tabs = tool_args.get("include_tabs", True)
            include_page_text = tool_args.get("include_page_text", False)
            text_limit = tool_args.get("text_limit", 1200)
            result = await browser_automation.get_state(
                include_tabs=include_tabs,
                include_page_text=include_page_text,
                text_limit=text_limit,
            )
            return json.dumps(result, indent=2)

        elif tool_name == "browser_disconnect":
            result = await browser_automation.disconnect_browser(
                terminate_launched=tool_args.get("terminate_launched", False)
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

        elif tool_name == "create_tool":
            overwrite_raw = tool_args.get("overwrite", False)
            if isinstance(overwrite_raw, bool):
                overwrite = overwrite_raw
            else:
                overwrite = str(overwrite_raw).strip().lower() in {"1", "true", "yes", "y", "on"}

            result = dynamic_tools.create_tool(
                name=str(tool_args.get("name", "")).strip(),
                description=str(tool_args.get("description", "")).strip(),
                command_template=str(tool_args.get("command_template", "")).strip(),
                parameters=tool_args.get("parameters"),
                required=tool_args.get("required"),
                timeout=tool_args.get("timeout", 30),
                guide=tool_args.get("guide", ""),
                overwrite=overwrite,
            )
            if result.get("ok"):
                _tools_guide_cache = None
            return json.dumps(result, indent=2)

        elif tool_name == "list_dynamic_tools":
            return json.dumps({"tools": dynamic_tools.list_tools()}, indent=2)

        elif tool_name == "delete_tool":
            result = dynamic_tools.delete_tool(str(tool_args.get("name", "")).strip())
            if result.get("ok"):
                _tools_guide_cache = None
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

        elif tool_name == "migrate_project":
            import shutil
            name = str(tool_args.get("name", "")).strip()
            source_path = str(tool_args.get("source_path", "")).strip()
            if not name:
                return json.dumps({"error": "No name provided"})
            if not source_path:
                return json.dumps({"error": "No source_path provided"})
            source_path = source_path if os.path.isabs(source_path) else os.path.join(_SCRIPT_DIR, source_path)
            source_path = os.path.realpath(source_path)
            if not os.path.isfile(source_path):
                return json.dumps({"error": f"Source file not found: {source_path}"}, indent=2)
            description = str(tool_args.get("description", "")).strip()
            reg = gateway.register_project(name, description=description)
            dest_path = os.path.join(reg["path"], "index.html")
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            try:
                shutil.copy2(source_path, dest_path)
            except Exception as e:
                return json.dumps({"error": f"Failed to copy file: {e}"}, indent=2)
            return json.dumps({
                "status": "live",
                "url": reg["url"],
                "share_this_link": reg["url"],
                "path": reg["path"],
                "source": source_path,
                "instruction": "Send the url to the user — this is the live clickable link to their project",
            }, indent=2)

        elif tool_name == "list_projects":
            return json.dumps({"projects": gateway.list_projects()}, indent=2)

        elif tool_name == "send_telegram_message":
            message = str(tool_args.get("message", "")).strip()
            if not message:
                return json.dumps({"error": "No message provided"})
            if send_func:
                await _send_via_send_func(send_func, message)
                return json.dumps({"sent": True})
            return json.dumps({"error": "No Telegram send function available"})

        elif tool_name == "send_voice_message":
            text = str(tool_args.get("text", "")).strip()
            if not text:
                return json.dumps({"error": "No text provided"})
            if len(text) > 500:
                text = text[:500]
            if send_func:
                await _send_via_send_func(send_func, text, voice=True)
                return json.dumps({"sent": True})
            return json.dumps({"error": "No Telegram send function available"})

        elif tool_name == "send_telegram_photo":
            path = str(tool_args.get("path", "")).strip()
            caption = str(tool_args.get("caption", "")).strip()
            if not path:
                return json.dumps({"error": "No path provided"})
            if not send_func:
                return json.dumps({"error": "No Telegram send function available"})

            resolved = _resolve_existing_image_path(path)
            if not resolved:
                return json.dumps({"error": "Image file not found", "path": path}, indent=2)

            await _send_photo_via_send_func(send_func, resolved, caption)
            return json.dumps({"sent": True, "path": resolved}, indent=2)

        elif tool_name == "send_telegram_document":
            path = str(tool_args.get("path", "")).strip()
            caption = str(tool_args.get("caption", "")).strip()
            if not path:
                return json.dumps({"error": "No path provided"})
            if not send_func:
                return json.dumps({"error": "No Telegram send function available"})

            resolved = _resolve_existing_image_path(path)
            if not resolved:
                return json.dumps({"error": "File not found", "path": path}, indent=2)

            await _send_document_via_send_func(send_func, resolved, caption)
            return json.dumps({"sent": True, "path": resolved}, indent=2)

        elif tool_name == "send_telegram_screenshot":
            source = str(tool_args.get("source", "browser")).strip().lower() or "browser"
            caption = str(tool_args.get("caption", "")).strip()
            output_path = str(tool_args.get("path", "")).strip() or None
            if not send_func:
                return json.dumps({"error": "No Telegram send function available"})

            if source == "browser":
                full_page_raw = tool_args.get("full_page", False)
                if isinstance(full_page_raw, bool):
                    full_page = full_page_raw
                else:
                    full_page = str(full_page_raw).strip().lower() in {"1", "true", "yes", "y", "on"}
                page_index = tool_args.get("page_index")
                if isinstance(page_index, (int, float)):
                    page_index = int(page_index)
                else:
                    page_index = None
                captured = await _capture_browser_screenshot(
                    path=output_path,
                    full_page=full_page,
                    page_index=page_index,
                )
            elif source == "screen":
                captured = _capture_screen_screenshot(path=output_path)
            else:
                return json.dumps({"error": "Invalid source. Use 'browser' or 'screen'."}, indent=2)

            if "error" in captured:
                return json.dumps(
                    {
                        "error": "Failed to capture screenshot",
                        "source": source,
                        "detail": captured,
                    },
                    indent=2,
                )

            screenshot_path = captured.get("path", "")
            if not screenshot_path:
                return json.dumps({"error": "Screenshot captured but path missing"}, indent=2)

            final_caption = caption or f"{source.capitalize()} screenshot"
            await _send_photo_via_send_func(send_func, screenshot_path, final_caption)
            return json.dumps(
                {
                    "sent": True,
                    "source": source,
                    "path": screenshot_path,
                },
                indent=2,
            )

        elif tool_name == "spawn_subagent":
            if not allow_subagent:
                return json.dumps({"error": "Nested subagent spawning is disabled"})
            role = str(tool_args.get("role", "general")).strip() or "general"
            task = str(tool_args.get("task", "")).strip()
            ctx = str(tool_args.get("context", "")).strip()
            if not task:
                return json.dumps({"error": "No task provided"})
            subagent_id = f"sub-{role}-{id(task) % 10000}"
            await _t.emit(
                "spawn",
                _agent_id,
                "Spawning subagent",
                f"{role}: {task[:100]}",
                _with_activity_meta({"role": role, "task": task, "subagent_id": subagent_id, "parent_agent": _agent_id}),
            )
            wall_timeout = max(30, min(int(_MAX_SUBAGENT_WALL_TIMEOUT_S), 900))
            try:
                result = await asyncio.wait_for(
                    _run_subagent(
                        user_id,
                        role,
                        task,
                        ctx,
                        _build_subagent_send_func(send_func),
                        _agent_id=subagent_id,
                        _parent_agent_id=_agent_id,
                    ),
                    timeout=wall_timeout,
                )
                await _t.emit(
                    "done",
                    subagent_id,
                    "Subagent finished",
                    f"{role}: {task[:80]}",
                    _with_activity_meta({"duration_ms": round((time.monotonic() - _tool_t0) * 1000), "role": role, "subagent_id": subagent_id, "parent_agent": _agent_id}),
                )
            except asyncio.TimeoutError:
                await _t.emit(
                    "error",
                    subagent_id,
                    "Subagent timed out",
                    f"{role}: {task[:80]} after {wall_timeout}s",
                    _with_activity_meta({"role": role, "subagent_id": subagent_id, "timeout_s": wall_timeout, "parent_agent": _agent_id}),
                )
                if send_func:
                    try:
                        await _send_via_send_func(
                            send_func,
                            f"Subagent timeout after {wall_timeout}s while working on '{task[:120]}'. "
                            f"Stopping now with failure instead of hanging. [{role}]"
                        )
                    except Exception:
                        pass
                return json.dumps(
                    {
                        "error": "Subagent timed out",
                        "role": role,
                        "timeout_s": wall_timeout,
                        "task": task,
                    },
                    indent=2,
                )
            return json.dumps({"role": role, "task": task, "result": result}, indent=2)

        elif tool_name == "read_tool_guide":
            name = str(tool_args.get("tool_name", "")).strip()
            if not name:
                return json.dumps({"error": "tool_name is required"}, indent=2)
            filepath = os.path.join(_TOOLS_DIR, f"{name}.md")
            if not os.path.isfile(filepath):
                available = [f[:-3] for f in os.listdir(_TOOLS_DIR) if f.endswith(".md")] if os.path.isdir(_TOOLS_DIR) else []
                return json.dumps({"error": f"Unknown tool: {name}", "available_tools": available}, indent=2)
            with open(filepath, "r") as f:
                content = f.read().strip()
            return content

        else:
            if dynamic_tools.get_tool_spec(tool_name):
                prepared = dynamic_tools.build_command(tool_name, tool_args)
                if prepared.get("error"):
                    return json.dumps(prepared, indent=2)

                command = str(prepared.get("command", "")).strip()
                timeout = prepared.get("timeout", 30)
                if not isinstance(timeout, (int, float)):
                    timeout = 30
                timeout = max(1, min(int(timeout), _MAX_COMMAND_TIMEOUT))

                if _is_passive_wait_command(command):
                    return json.dumps(
                        {
                            "error": "Passive wait commands are blocked",
                            "hint": "Edit the dynamic tool command to do real work instead of sleep/timeout loops.",
                        },
                        indent=2,
                    )

                result = await terminal_tools.execute_command(command, timeout=timeout)
                return json.dumps(
                    {
                        "tool": tool_name,
                        "command": command,
                        "result": result,
                    },
                    indent=2,
                )

            return json.dumps({"error": f"Unknown tool: {tool_name}"})

    except Exception as e:
        logger.exception(f"Tool call error: {tool_name}")
        _tool_elapsed = (time.monotonic() - _tool_t0) * 1000
        await _t.emit("error", _agent_id, f"Tool error: {tool_name}", str(e)[:200], _with_activity_meta({"duration_ms": round(_tool_elapsed), "tool": tool_name}))
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Agentic loop (multi-turn tool calling with parallel subagents)
# ---------------------------------------------------------------------------


_TOOLCALL_TAG_RE = re.compile(
    r"<toolcall>(.+?)(?:</toolcall>|$)",
    re.DOTALL,
)
_TOOLCALL_ARG_RE = re.compile(
    r'\$(\w+)=("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|\[[^\]]*\]|\{[^}]*\}|[^\s<]+)',
)
_TOOLCALL_NAME_RE = re.compile(r"^([a-zA-Z_][\w:]*)")
_TOOLCALL_JSON_RE = re.compile(
    r"`(?:json)?\s*(\{.+?\})\s*$",
    re.DOTALL,
)
_TOOLCALL_ARTIFACT_RE = re.compile(
    r"</?(?:toolcall|argvalue|argkey)\s*>",
    re.IGNORECASE,
)
_ARGKEY_VALUE_RE = re.compile(
    r"<argkey>\s*(\w+)\s*</argkey>\s*<argvalue>\s*(.+?)\s*</argvalue>",
    re.DOTALL,
)
_ARGVALUE_ONLY_RE = re.compile(
    r"<argvalue>\s*(.+?)\s*</argvalue>",
    re.DOTALL,
)
_SINGLE_ARG_HINT = {
    "execute_command": "command",
    "command": "command",
    "read_file": "path",
    "readfile": "path",
    "write_file": "path",
    "writefile": "path",
    "web_search": "query",
    "scrape_url": "url",
    "send_telegram_message": "message",
    "send_voice_message": "text",
    "read_tool_guide": "tool_name",
    "search_memories": "query",
    "list_memories": "category",
}

_TEXT_TOOL_ALIASES = {
    "command": "execute_command",
    "readfile": "read_file",
    "writefile": "write_file",
    "editfile": "edit_file",
    "websearch": "web_search",
    "scrape": "scrape_url",
}


def _sanitize_response(text: str) -> str:
    if not text:
        return text
    cleaned = _TOOLCALL_ARTIFACT_RE.sub("", text)
    if cleaned != text:
        logger.warning(f"Stripped tool-call artifacts from response: {text[:200]}")
    return cleaned.strip()


def _parse_text_tool_calls(content: str) -> tuple[list[dict], str]:
    parsed = []
    remaining = _TOOLCALL_TAG_RE.sub("", content)
    for match in _TOOLCALL_TAG_RE.finditer(content):
        body = match.group(1).strip()
        if not body:
            continue
        name_match = _TOOLCALL_NAME_RE.match(body)
        if not name_match:
            continue
        raw_name = name_match.group(1)
        tool_name = raw_name.split(":", 1)[-1] if ":" in raw_name else raw_name
        tool_name = _TEXT_TOOL_ALIASES.get(tool_name, tool_name)
        rest = body[name_match.end():].strip()
        rest = re.sub(r"</?argkey\s*>", "", rest)

        args: dict = {}

        json_match = _TOOLCALL_JSON_RE.search(rest)
        if json_match:
            try:
                args = json.loads(json_match.group(1))
                if not isinstance(args, dict):
                    args = {}
            except (json.JSONDecodeError, TypeError):
                logger.warning(f"Failed to parse JSON args in text tool call: {json_match.group(1)[:200]}")
        elif "<argvalue>" in rest:
            for kv_match in _ARGKEY_VALUE_RE.finditer(rest):
                args[kv_match.group(1).strip()] = kv_match.group(2).strip()
            if not args:
                values = [m.group(1).strip() for m in _ARGVALUE_ONLY_RE.finditer(rest)]
                if values:
                    hint = _SINGLE_ARG_HINT.get(tool_name)
                    if hint:
                        args[hint] = values[0]
                    elif len(values) == 1:
                        args["arg"] = values[0]
                    else:
                        for i, v in enumerate(values):
                            args[f"arg{i}"] = v
        else:
            for arg_match in _TOOLCALL_ARG_RE.finditer(rest):
                key = arg_match.group(1)
                val_str = arg_match.group(2)
                try:
                    val = json.loads(val_str)
                except (json.JSONDecodeError, TypeError):
                    val = val_str.strip("'\"")
                args[key] = val

        parsed.append({
            "id": f"text_tool_{len(parsed)}",
            "name": tool_name,
            "arguments": args,
        })

    if parsed:
        logger.info(f"Parsed {len(parsed)} text tool call(s): {[tc['name'] for tc in parsed]}")
    return parsed, remaining.strip()


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
    interrupt_event: asyncio.Event | None = None,
    interrupt_queue: asyncio.Queue | None = None,
    _agent_id: str = "orchestrator",
    _parent_agent_id: str | None = None,
) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": prompt})

    using_default_tools = tools is None
    if using_default_tools:
        tools = _get_all_tools(include_subagent=allow_subagent, include_telegram=send_func is not None)

    model_id = _MODELS.get(model.lower(), model)

    def _with_activity_meta(extra: dict | None = None) -> dict:
        payload = dict(extra or {})
        if _parent_agent_id:
            payload.setdefault("parent_agent", _parent_agent_id)
        return payload

    def _drain_interrupts() -> list[dict]:
        if not interrupt_event or not interrupt_queue:
            return []
        items = []
        while not interrupt_queue.empty():
            try:
                item = interrupt_queue.get_nowait()
                items.append(item)
            except asyncio.QueueEmpty:
                break
        if items:
            interrupt_event.clear()
        return items

    def _build_interruption_message(items: list[dict]) -> dict | None:
        if not items:
            return None
        user_texts = [i.get("text", "") for i in items if i.get("source") != "stuck_watchdog"]
        system_texts = [i.get("text", "") for i in items if i.get("source") == "stuck_watchdog"]

        parts = []
        if user_texts:
            combined = "\n".join(f"- {t}" for t in user_texts)
            parts.append(
                "[USER INTERRUPTION] The user sent new message(s) while you were working:\n\n"
                f"{combined}\n\n"
                "Decide: adjust current task, send updated instructions to subagents, "
                "cancel and pivot, or acknowledge and continue."
            )
        if system_texts:
            combined = "\n".join(f"- {t}" for t in system_texts)
            parts.append(
                "[SYSTEM INTERVENTION] The watchdog detected you have been stuck with no progress. "
                "You MUST stop waiting on whatever is hung and try a different approach immediately. "
                "Do not ignore this.\n\n"
                f"{combined}"
            )

        if not parts:
            return None
        return {
            "role": "user",
            "content": "\n\n".join(parts),
        }

    for _ in range(max_rounds):
        interrupts = _drain_interrupts()
        if interrupts:
            msg = _build_interruption_message(interrupts)
            if msg:
                messages.append(msg)

        _t = activity_tracker.get_tracker()
        await _t.emit("thinking", _agent_id, "Thinking", f"Round {_ + 1}/{max_rounds} — calling model {model_id}", _with_activity_meta())

        t0 = time.monotonic()
        response = await model_router.call_model(model_id, messages, tools)
        elapsed = (time.monotonic() - t0) * 1000

        tool_calls = response.get("tool_calls", [])
        content_text = response.get("content", "")
        text_tools = None

        if not tool_calls:
            text_tools, cleaned_content = _parse_text_tool_calls(content_text)
            if text_tools:
                tool_calls = text_tools
                content_text = cleaned_content
                logger.warning(f"Model emitted {len(text_tools)} tool call(s) as text, intercepted and routing to execution")
                messages.append({"role": "assistant", "content": content_text})
            else:
                await _t.emit("done", _agent_id, "Responded", f"Final answer in {elapsed:.0f}ms", _with_activity_meta({"duration_ms": round(elapsed)}))
                interrupt_texts = _drain_interrupts()
                if interrupt_texts:
                    msg = _build_interruption_message(interrupt_texts)
                    if msg:
                        messages.append(response.get("message", {"role": "assistant", "content": response.get("content", "")}))
                        messages.append(msg)
                        continue
                return _sanitize_response(response.get("content", "I could not generate a response right now."))
        
        if not text_tools:
            messages.append(response["message"])
        
        tool_calls = tool_calls[:max_calls_per_round]
        
        if tool_calls:
            names = [tc["name"] for tc in tool_calls]
            await _t.emit("model", _agent_id, "Model decided", f"{len(tool_calls)} tool(s): {', '.join(names)}", _with_activity_meta({"duration_ms": round(elapsed), "model": model_id}))
        
        subagent_calls = [tc for tc in tool_calls if tc["name"] == "spawn_subagent"]
        other_calls = [tc for tc in tool_calls if tc["name"] != "spawn_subagent"]
        
        results: dict[str, str] = {}
        
        for tc in other_calls:
            results[tc["id"]] = await _execute_tool_call(
                tc["name"], tc["arguments"],
                user_id if user_id is not None else 0,
                send_func=send_func,
                allow_subagent=allow_subagent,
                _agent_id=_agent_id,
                _parent_agent_id=_parent_agent_id,
            )
        
        if subagent_calls:
            async def _do_subagent(tc):
                r = await _execute_tool_call(
                    tc["name"], tc["arguments"],
                    user_id if user_id is not None else 0,
                    send_func=send_func,
                    allow_subagent=allow_subagent,
                    _agent_id=_agent_id,
                    _parent_agent_id=_parent_agent_id,
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

        if using_default_tools and any(tc["name"] in {"create_tool", "delete_tool"} for tc in tool_calls):
            tools = _get_all_tools(include_subagent=allow_subagent, include_telegram=send_func is not None)
    
    try:
        interrupt_texts = _drain_interrupts()
        if interrupt_texts:
            msg = _build_interruption_message(interrupt_texts)
            if msg:
                messages.append(msg)
        response = await model_router.call_model(model_id, messages, None)
        final_content = response.get("content", "I could not generate a response right now.")

        interrupt_texts = _drain_interrupts()
        if interrupt_texts:
            msg = _build_interruption_message(interrupt_texts)
            if msg:
                messages.append(response.get("message", {"role": "assistant", "content": final_content}))
                messages.append(msg)
                response = await model_router.call_model(model_id, messages, None)
                final_content = response.get("content", final_content)

        return _sanitize_response(final_content)
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
    _agent_id: str = "subagent",
    _parent_agent_id: str | None = None,
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
        _agent_id=_agent_id,
        _parent_agent_id=_parent_agent_id,
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

async def respond(
    user_id: int,
    text: str,
    send_func: SendFunc | None = None,
    interrupt_event: asyncio.Event | None = None,
    interrupt_queue: asyncio.Queue | None = None,
    model_override: str | None = None,
) -> str:
    _t = activity_tracker.get_tracker()
    await _t.emit("receive", "orchestrator", "Message received", text[:200])
    model = model_override or db.get_model(user_id)
    history = db.get_history(user_id)
    system = _build_system(user_id, text)
    
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    
    recent_image_msg = None
    for msg in reversed(history[-10:]):
        if msg.get("image_b64"):
            recent_image_msg = msg
            break
    
    if recent_image_msg:
        vision_model = db.get_image_model(user_id)
        vision_model_id = _MODELS.get(vision_model.lower(), vision_model)
        
        image_content = {
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{recent_image_msg['image_b64']}"}
        }
        
        image_context_prompt = f"""The user previously sent an image with the message: "{recent_image_msg['content']}"

Now they're asking: "{text}"

Please re-analyze the image in light of this new question, then respond to their current request."""

        user_message = {
            "role": "user",
            "content": [
                {"type": "text", "text": image_context_prompt},
                image_content
            ]
        }
        
        for msg in history:
            if msg.get("image_b64"):
                messages.append({"role": msg["role"], "content": msg["content"]})
            else:
                messages.append(msg)
        
        messages.append(user_message)
        
        try:
            response = await model_router.call_model(vision_model_id, messages, None)
            image_analysis = response.get("content") or "I could not analyze the image."
        except Exception as e:
            logger.exception(f"Image re-analysis failed for user {user_id}")
            image_analysis = f"Error re-analyzing image: {e}"
        
        agent_prompt = f"""The user previously sent an image and is now asking a follow-up question.

Previous image message: "{recent_image_msg['content']}"
Current question: "{text}"

Here is my re-analysis of the image based on the new question:
{image_analysis}

Based on this analysis and the user's current request, proceed with any tasks needed."""
        
        orchestrator_timeout = max(60, min(int(_MAX_ORCHESTRATOR_WALL_TIMEOUT_S), 1800))
        try:
            reply = await asyncio.wait_for(
                _run_agent(
                    model, agent_prompt,
                    system=system,
                    history=history,
                    tools=_get_all_tools(include_subagent=True, include_telegram=send_func is not None),
                    user_id=user_id,
                    send_func=send_func,
                    allow_subagent=True,
                    interrupt_event=interrupt_event,
                    interrupt_queue=interrupt_queue,
                ),
                timeout=orchestrator_timeout,
            )
        except asyncio.TimeoutError:
            reply = (
                f"I hit the processing timeout ({orchestrator_timeout}s) and stopped to avoid hanging. "
                "Please retry with a narrower request."
            )
        except Exception as e:
            logger.exception(f"Agent processing failed after image re-analysis for user {user_id}")
            reply = f"Image re-analyzed, but processing failed: {e}\n\nImage analysis: {image_analysis}"
        
        if not reply:
            reply = image_analysis
    else:
        orchestrator_timeout = max(60, min(int(_MAX_ORCHESTRATOR_WALL_TIMEOUT_S), 1800))
        try:
            reply = await asyncio.wait_for(
                _run_agent(
                    model, text,
                    system=system,
                    history=history,
                    tools=_get_all_tools(include_subagent=True, include_telegram=send_func is not None),
                    user_id=user_id,
                    send_func=send_func,
                    allow_subagent=True,
                    interrupt_event=interrupt_event,
                    interrupt_queue=interrupt_queue,
                ),
                timeout=orchestrator_timeout,
            )
        except asyncio.TimeoutError:
            reply = (
                f"I hit the processing timeout ({orchestrator_timeout}s) and stopped to avoid hanging. "
                "Please retry with a narrower request."
            )

    db.add_message(user_id, "user", text)
    db.add_message(user_id, "assistant", reply)
    await _maybe_summarize(user_id, model)
    return _sanitize_response(reply)


async def respond_with_image(user_id: int, text: str, image_b64: str, send_func: SendFunc | None = None) -> str:
    vision_model = db.get_image_model(user_id)
    main_model = db.get_model(user_id)
    system = _build_system(user_id, text, include_memories=True)

    image_content = {
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}
    }
    user_message = {
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            image_content
        ]
    }

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    
    history = db.get_history(user_id)
    if history:
        for msg in history[-10:]:
            messages.append(msg)
    
    messages.append(user_message)

    vision_model_id = _MODELS.get(vision_model.lower(), vision_model)

    try:
        response = await model_router.call_model(vision_model_id, messages, None)
        image_analysis = response.get("content") or "I could not analyze the image."
    except Exception as e:
        logger.exception(f"Image analysis failed for user {user_id}")
        image_analysis = f"Error analyzing image: {e}"

    orchestrator_timeout = max(60, min(int(_MAX_ORCHESTRATOR_WALL_TIMEOUT_S), 1800))
    try:
        agent_prompt = f"""The user sent an image with the message: "{text}"

Here is my analysis of the image:
{image_analysis}

Based on this analysis and the user's request, proceed with any tasks needed. If the user is asking for something to be created or done, use the available tools to accomplish it."""
        
        reply = await asyncio.wait_for(
            _run_agent(
                main_model, agent_prompt,
                system=system,
                history=history,
                tools=_get_all_tools(include_subagent=True, include_telegram=send_func is not None),
                user_id=user_id,
                send_func=send_func,
                allow_subagent=True,
            ),
            timeout=orchestrator_timeout,
        )
    except asyncio.TimeoutError:
        reply = (
            f"I hit the processing timeout ({orchestrator_timeout}s) and stopped to avoid hanging. "
            "Please retry with a narrower request."
        )
    except Exception as e:
        logger.exception(f"Agent processing failed after image analysis for user {user_id}")
        reply = f"Image analyzed, but processing failed: {e}\n\nImage analysis: {image_analysis}"

    if not reply:
        reply = image_analysis

    db.add_message(user_id, "user", f"[Image] {text}", image_b64=image_b64)
    db.add_message(user_id, "assistant", reply)
    await _maybe_summarize(user_id, main_model)
    return _sanitize_response(reply)
