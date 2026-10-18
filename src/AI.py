import os
import sys
import asyncio
import json
import logging
import re
import subprocess
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Callable, Awaitable
from dotenv import load_dotenv
import app_paths
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
import homeassistant
import pa_system
import panel_popups
import activity_tracker
import interrupt_hub
import laya_decisions

app_paths.ensure_runtime_dirs()
load_dotenv(dotenv_path=app_paths.env_file_path())

logger = logging.getLogger("talos.ai")

_RESOURCE_DIR = app_paths.resource_root()
_DATA_DIR = app_paths.data_root()
_TOOLS_DIR = app_paths.tools_resource_dir()
_DYNAMIC_TOOLS_DIR = app_paths.dynamic_tools_docs_dir()
_tools_guide_cache: str | None = None

_MODELS = model_router.get_all_model_aliases()

_MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "40"))
_MAX_TOOL_CALLS_PER_ROUND = int(os.getenv("MAX_TOOL_CALLS_PER_ROUND", "30"))
_MAX_COMMAND_TIMEOUT = int(os.getenv("MAX_COMMAND_TIMEOUT", "300"))
_MAX_WORKFLOW_STEPS = int(os.getenv("MAX_WORKFLOW_STEPS", "30"))
_MAX_ORCHESTRATOR_WALL_TIMEOUT_S = int(os.getenv("MAX_ORCHESTRATOR_WALL_TIMEOUT_S", "1800"))
_MAX_SUBAGENT_TOOL_ROUNDS = int(os.getenv("MAX_SUBAGENT_TOOL_ROUNDS", "20"))
_MAX_SUBAGENT_TOOL_CALLS_PER_ROUND = int(os.getenv("MAX_SUBAGENT_TOOL_CALLS_PER_ROUND", "25"))
_MAX_SUBAGENT_WALL_TIMEOUT_S = int(os.getenv("MAX_SUBAGENT_WALL_TIMEOUT_S", "900"))
_SUBAGENT_MAX_TELEGRAM_MESSAGES = int(os.getenv("SUBAGENT_MAX_TELEGRAM_MESSAGES", "3"))
_SUBAGENT_MAX_TELEGRAM_MESSAGE_CHARS = int(os.getenv("SUBAGENT_MAX_TELEGRAM_MESSAGE_CHARS", "260"))
_SUBAGENT_MIN_UPDATE_INTERVAL_S = float(os.getenv("SUBAGENT_MIN_UPDATE_INTERVAL_S", "30"))

# --- Long-running task knobs ------------------------------------------------
# A single tool result this big is truncated before it enters the run's
# context. Big file reads / command dumps are what actually kill long tasks:
# the model drowns in its own output long before it runs out of rounds.
_MAX_TOOL_RESULT_CHARS = int(os.getenv("MAX_TOOL_RESULT_CHARS", "12000"))
# When the run's accumulated context passes this, older tool results are
# compacted (kept as short stubs) so the run can keep going indefinitely.
_MAX_RUN_CONTEXT_CHARS = int(os.getenv("MAX_RUN_CONTEXT_CHARS", "160000"))
# How many fresh round budgets an agent earns by making progress. With
# MAX_TOOL_ROUNDS=40 and 6 continuations, a productive task gets up to 280
# rounds instead of dying at 40 with "break the task into smaller pieces".
_MAX_TASK_CONTINUATIONS = int(os.getenv("MAX_TASK_CONTINUATIONS", "6"))
# Concurrent background subagents the orchestrator may have in flight.
_BACKGROUND_SUBAGENT_MAX = int(os.getenv("BACKGROUND_SUBAGENT_MAX", "8"))
# Raid mode lifts the subagent ceiling: the swarm is bounded only by how much
# work the task actually has. Token-heavy by design.
RAID_BACKGROUND_SUBAGENT_MAX = int(os.getenv("RAID_BACKGROUND_SUBAGENT_MAX", "100"))


def stop_background_subagents(user_id: int | None = None) -> int:
    """Cancel running background subagents — the emergency stop for swarms.

    Returns how many were stopped. With user_id, only that user's subagents
    are cancelled (legacy entries without a user are included).
    """
    stopped = 0
    for entry in list(_BACKGROUND_SUBAGENTS.values()):
        if entry.get("status") != "running":
            continue
        owner = entry.get("user_id")
        if user_id is not None and owner is not None and owner != user_id:
            continue
        task = entry.get("task")
        if task is not None and not task.done():
            task.cancel()
            stopped += 1
        entry["status"] = "cancelled"
        entry["error"] = "Cancelled by the user (stop)."
    if stopped:
        logger.info("Stopped %d background subagent(s).", stopped)
    return stopped


def background_subagent_cap(raid: bool = False) -> int:
    return max(1, RAID_BACKGROUND_SUBAGENT_MAX if raid else _BACKGROUND_SUBAGENT_MAX)

_VALID_SPEED_MODES = {"quick", "fast", "normal"}

SendFunc = Callable[..., Awaitable[None]]


def _normalize_speed_mode(speed_mode: str | None) -> str:
    mode = str(speed_mode or "").strip().lower()
    if mode in _VALID_SPEED_MODES:
        return mode
    return "normal"


def _pick_runtime_model(base_model: str, speed_mode: str) -> str:
    mode = _normalize_speed_mode(speed_mode)
    if mode != "quick":
        return base_model

    fast_model = os.getenv("FAST_MODEL", "").strip()
    if not fast_model:
        return base_model

    provider, _ = model_router.resolve_model(fast_model)
    if model_router._provider_enabled(provider):
        return fast_model
    return base_model


def _agent_limits_for_speed(speed_mode: str, raid: bool = False) -> tuple[int, int, int | None]:
    rounds = max(1, _MAX_TOOL_ROUNDS)
    calls = max(1, _MAX_TOOL_CALLS_PER_ROUND)
    if raid:
        # Raid mode: the orchestrator coordinates a swarm, which means many
        # more spawn calls per response and longer runs. Doubled, not uncapped,
        # so a confused model cannot loop forever.
        rounds *= 2
        calls = max(calls, 50)
    if os.getenv("ORCHESTRATOR_TIMEOUT_ENABLED", "1") == "0":
        timeout = None  # Read live from env so Settings changes apply without a restart.
    else:
        timeout = max(60, min(int(_MAX_ORCHESTRATOR_WALL_TIMEOUT_S), 86400))
    return rounds, calls, timeout


def _subagent_limits_for_speed(speed_mode: str, raid: bool = False) -> tuple[int, int]:
    rounds = max(1, _MAX_SUBAGENT_TOOL_ROUNDS)
    calls = max(1, _MAX_SUBAGENT_TOOL_CALLS_PER_ROUND)
    if raid:
        rounds *= 2
        calls *= 2
    return rounds, calls


def reload_clients():
    global _tools_guide_cache
    global _MAX_TOOL_ROUNDS, _MAX_TOOL_CALLS_PER_ROUND
    global _MAX_COMMAND_TIMEOUT, _MAX_WORKFLOW_STEPS, _MAX_ORCHESTRATOR_WALL_TIMEOUT_S
    global _MAX_SUBAGENT_TOOL_ROUNDS, _MAX_SUBAGENT_TOOL_CALLS_PER_ROUND, _MAX_SUBAGENT_WALL_TIMEOUT_S
    global _SUBAGENT_MAX_TELEGRAM_MESSAGES, _SUBAGENT_MAX_TELEGRAM_MESSAGE_CHARS, _SUBAGENT_MIN_UPDATE_INTERVAL_S
    global _MAX_AGENT_TELEGRAM_MESSAGES, _MAX_IDENTICAL_TOOL_CALLS, _MAX_STALLED_ROUNDS
    global _MAX_TOOL_RESULT_CHARS, _MAX_RUN_CONTEXT_CHARS, _MAX_TASK_CONTINUATIONS, _BACKGROUND_SUBAGENT_MAX
    _tools_guide_cache = None
    model_router.reload_clients()
    load_dotenv(dotenv_path=app_paths.env_file_path(), override=True)
    _MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "40"))
    _MAX_TOOL_CALLS_PER_ROUND = int(os.getenv("MAX_TOOL_CALLS_PER_ROUND", "30"))
    _MAX_COMMAND_TIMEOUT = int(os.getenv("MAX_COMMAND_TIMEOUT", "300"))
    _MAX_WORKFLOW_STEPS = int(os.getenv("MAX_WORKFLOW_STEPS", "30"))
    _MAX_ORCHESTRATOR_WALL_TIMEOUT_S = int(os.getenv("MAX_ORCHESTRATOR_WALL_TIMEOUT_S", "1800"))
    _MAX_SUBAGENT_TOOL_ROUNDS = int(os.getenv("MAX_SUBAGENT_TOOL_ROUNDS", "20"))
    _MAX_SUBAGENT_TOOL_CALLS_PER_ROUND = int(os.getenv("MAX_SUBAGENT_TOOL_CALLS_PER_ROUND", "25"))
    _MAX_SUBAGENT_WALL_TIMEOUT_S = int(os.getenv("MAX_SUBAGENT_WALL_TIMEOUT_S", "900"))
    _MAX_TOOL_RESULT_CHARS = int(os.getenv("MAX_TOOL_RESULT_CHARS", "12000"))
    _MAX_RUN_CONTEXT_CHARS = int(os.getenv("MAX_RUN_CONTEXT_CHARS", "160000"))
    _MAX_TASK_CONTINUATIONS = int(os.getenv("MAX_TASK_CONTINUATIONS", "6"))
    _BACKGROUND_SUBAGENT_MAX = int(os.getenv("BACKGROUND_SUBAGENT_MAX", "8"))
    _SUBAGENT_MAX_TELEGRAM_MESSAGES = int(os.getenv("SUBAGENT_MAX_TELEGRAM_MESSAGES", "3"))
    _SUBAGENT_MAX_TELEGRAM_MESSAGE_CHARS = int(os.getenv("SUBAGENT_MAX_TELEGRAM_MESSAGE_CHARS", "260"))
    _SUBAGENT_MIN_UPDATE_INTERVAL_S = float(os.getenv("SUBAGENT_MIN_UPDATE_INTERVAL_S", "30"))
    _MAX_AGENT_TELEGRAM_MESSAGES = int(os.getenv("MAX_AGENT_TELEGRAM_MESSAGES", "8"))
    _MAX_IDENTICAL_TOOL_CALLS = int(os.getenv("MAX_IDENTICAL_TOOL_CALLS", "1"))
    _MAX_STALLED_ROUNDS = int(os.getenv("MAX_STALLED_ROUNDS", "2"))


def list_models() -> list[str]:
    return model_router.list_models_with_provider()


def _load_tools_guide() -> str:
    global _tools_guide_cache
    if _tools_guide_cache is not None:
        return _tools_guide_cache
    lines = []
    seen_tools: set[str] = set()
    for docs_dir in [_TOOLS_DIR, _DYNAMIC_TOOLS_DIR]:
        if not os.path.isdir(docs_dir):
            continue
        for filename in sorted(os.listdir(docs_dir)):
            if not filename.endswith(".md"):
                continue
            tool_name = filename[:-3]
            if tool_name in seen_tools:
                continue
            filepath = os.path.join(docs_dir, filename)
            try:
                with open(filepath, "r") as f:
                    first_lines = [f.readline() for _ in range(5)]
                desc = ""
                for line in first_lines[1:]:
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#"):
                        desc = stripped
                        break
                lines.append(f"- {tool_name}: {desc}" if desc else f"- {tool_name}")
                seen_tools.add(tool_name)
            except Exception:
                pass
    summary = "You have access to the following tools. Use `read_tool_guide` to read the full usage guide for any tool before using it.\n\n" + "\n".join(lines) if lines else ""
    _tools_guide_cache = summary
    return _tools_guide_cache


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _is_email_configured() -> bool:
    config_path = str(os.getenv("HIMALAYA_CONFIG", "")).strip().strip('"')
    if not config_path:
        return False

    expanded = os.path.expanduser(config_path)
    candidates: list[str] = []

    if os.path.isabs(expanded):
        candidates.append(expanded)
    else:
        candidates.extend([
            os.path.join(os.getcwd(), expanded),
            os.path.join(app_paths.data_root(), expanded),
            os.path.join(app_paths.source_root(), expanded),
        ])

    for candidate in candidates:
        if os.path.isfile(os.path.realpath(candidate)):
            return True
    return False


# The raid doctrine: aggressive parallelism, but with the orchestration rules
# that keep a 20-100 agent swarm coherent instead of a token bonfire.
_RAID_MODE_PROMPT = (
    "[RAID MODE ACTIVE — swarm orchestration]\n"
    "The user has lifted your subagent limits and asked for maximum speed on a large task. "
    "Token cost is not a constraint right now; speed and coverage are. Build an army — but a "
    "DISCIPLINED one. A swarm without orchestration is just noise.\n\n"
    "PLAN BEFORE YOU SPAWN:\n"
    "0. CONFIRM FIRST: before spawning anything, send the user ONE short message stating "
    "what you understood the task to be, the division of labor you are about to launch "
    "(roles and what each covers), and what they will get when the raid completes — then "
    "start spawning in the SAME response. The confirmation is how the user catches a "
    "misunderstanding before tokens burn on it.\n"
    "1. Decompose the task into non-overlapping specializations BEFORE spawning anything. "
    "Decide the division of labor first: who covers what, and what is explicitly out of each "
    "agent's scope. Overlap is waste; gaps are failure. If two agents would produce the same "
    "thing, the split is wrong — re-split before spawning.\n"
    "2. Scale the army to the task: a big multi-faceted task deserves 20-100 specialized "
    "subagents; a modest one deserves 3-6. Spawn the SMALLEST army that fully covers the "
    "task — do not pad with duplicate roles.\n"
    "3. Use waves: a first wave of parallel scouts/researchers (background=true for anything "
    "long, foreground batches for short passes), then synthesize what came back, then a second "
    "wave of executors with the refined context. Waves, not one mob.\n"
    "4. Every subagent gets: a sharp role label, a task with explicit scope boundaries, and "
    "the context it needs. Specify exactly which files/sites/sources/aspects belong to it — "
    "and tell it NOT to touch the others.\n\n"
    "YOU ARE THE CONDUCTOR — NOT THE PRODUCER:\n"
    "- Every deliverable is made by a specialist subagent: documents by a writer agent, "
    "pitch decks by a deck agent, websites/code by builder agents, reports by the agents "
    "that did the research. You do NOT write deliverables yourself — doing the work "
    "yourself while agents idle is a raid failure.\n"
    "- In raid you push work down HARDER than normal: if any step of the plan has YOU "
    "executing it, you are under-spawning. Spawn another agent instead. Idle orchestrator "
    "rounds are the only waste a raid cannot afford.\n"
    "- DOUBLE-PUSH critical paths: for scopes the whole task depends on, spawn TWO agents "
    "with different approaches in parallel and keep the better result — redundant coverage "
    "beats serial precision in raid mode.\n"
    "- Your work is: decompose, assign with clear requirements, track, verify each result "
    "against the goal, and send revision notes back to the producing agent when the output "
    "misses the mark. Never rewrite an agent's deliverable yourself — send it back with "
    "specific fixes. You may merge and polish SEVERAL agents' finished pieces into the "
    "final reply, but new substantive content is always produced by a subagent.\n"
    "- Track who is running (subagent_status) and what each scope was. Reconcile conflicts "
    "between agents yourself; when two results disagree, decide or spawn one tie-breaker — "
    "do not let agents argue with each other.\n"
    "- Synthesize progressively: as results land, assemble the whole; do not sit idle waiting "
    "for everything if you can start integrating.\n"
    "- If a subagent fails or stalls, respawn just that scope with a clearer task — never "
    "restart the whole swarm.\n\n"
    "SUBAGENT DISCIPLINE (critical):\n"
    "- Subagents report to YOU through their final result — they do NOT message the user. "
    "You decide what the user hears and when: brief milestone updates from you, one synthesis "
    "at the end. A wall of per-agent messages is a raid failure, not a raid feature.\n"
    "- Their final results should be complete enough for you to integrate without re-asking.\n"
    "- If a subagent genuinely needs the user (missing credential, destructive action "
    "approval), it must say so in its result and YOU relay the question — batch questions "
    "from multiple agents into one message when possible.\n\n"
    "Raid ends when the task is done: synthesize all results into one coherent, complete "
    "answer for the user, noting which parts of the army produced what."
)


def _build_system(
    user_id: int,
    current_message: str = "",
    include_memories: bool = True,
    relevant_memories: list[dict] | None = None,
    raid_mode: bool = False,
) -> str | None:
    parts = []
    prompt = db.read_system_prompt()
    if prompt:
        parts.append(prompt)

    parts.append(f"[Current time] {_now_str()}")

    tools_guide = _load_tools_guide()
    if tools_guide:
        parts.append(f"[Available Tools]\n{tools_guide}")

    if _is_email_configured():
        parts.append("[Email] Email is configured and available. Use the email_execute tool for all email operations (send, read, reply, list). Do NOT tell the user email is not configured — it is.")

    env_context = environment.get_environment_context()
    if env_context:
        parts.append(f"[Environment]\n{env_context}")
    tg_format = environment.get_telegram_formatting_guide()
    if tg_format:
        parts.append(tg_format)

    parts.append(
        "[Orchestrator Instructions]\n"
        "You are the orchestrator — a CONDUCTOR, not a worker. Your job: understand the "
        "request, delegate ALL work to subagents, verify their results, and relay. Doing "
        "work yourself — even simple work — is a failure of your role, because it blocks "
        "you from handling the user's next message while a subagent could have been "
        "working.\n\n"
        "DELEGATE EVERYTHING:\n"
        "- ANY task that produces or changes anything is a subagent's job: a slideshow "
        "(spawn a deck-builder that calls create_project and returns the live URL), a "
        "document, code, research, a lookup, an email, a file edit, an answer that needs "
        "tools. Even simple one-step tasks: spawn a subagent to do and deliver it.\n"
        "- You only answer directly for pure conversation that needs no tools — greetings, "
        "clarifying questions, small talk about the task in hand.\n"
        "- If you catch yourself writing a file, building HTML, drafting content, or "
        "running a multi-step tool sequence: STOP. That is a subagent's job. Spawn one "
        "with the exact instructions you were about to follow yourself.\n"
        "- Free yourself: for self-contained work, spawn with background=true and END your "
        "turn after the confirmation — results are delivered when they finish, and you "
        "stay available for the user's next message. Use foreground batches only when "
        "you need the results to compose THIS reply.\n\n"
        "PARALLELIZE BY DEFAULT:\n"
        "- If a request has two or more independent parts (several files, pages, sources, topics, "
        "items, or questions), spawn one subagent per part IN THE SAME RESPONSE — they run in "
        "PARALLEL and you get all results together. Sequential handling of independent parts "
        "wastes the user's time.\n"
        "- Split compound requests at their natural seams: 'find X and do Y' → researcher finds X "
        "while you set up Y; 'compare A, B, and C' → three parallel researchers; 'refactor these "
        "files' → one subagent per file or per coherent group; 'research, then write, then "
        "review' → delegate the research, do the writing, delegate the review.\n"
        "- Delegate anything focused that would otherwise occupy your rounds: research, data "
        "gathering, verification, review, testing, drafting sections, and the PRODUCTION of "
        "deliverables (documents, decks, sites, reports) — you keep the "
        "planning, the assignment, and the verification.\n"
        "- For LONG-RUNNING subtasks (over ~15 minutes), use background=true: they run detached "
        "while you continue other work, and results are delivered to you when they finish.\n"
        "- When in doubt between doing it yourself and delegating: delegate. A subagent that "
        "turns out unnecessary costs a little; serial handling of parallelizable work costs "
        "the user's time every round.\n"
        "- Foreground subagents wait for you in one batch; background ones (up to 8) run "
        "alongside. Use subagent_status to check on background work.\n"
        "After all subagents complete, synthesize their results into one coherent response. "
        "Keep the user informed: when spawning subagents, say what you're delegating and why.\n\n"
        "CONFIRM BEFORE YOU WORK:\n"
        "- Before starting any multi-step task (anything needing 2+ tool calls, subagents, "
        "or more than a minute of work), FIRST send the user ONE short acknowledgment of "
        "what you understood and how you'll approach it, then immediately start working — "
        "ideally in the SAME response as your first spawn calls. "
        "Example: 'Got it — I'll audit the repo with three parallel agents (frontend, "
        "backend, tests) and send you a ranked cleanup list when they finish.'\n"
        "- Cover three things in 2-4 sentences: what you understood the request to be, "
        "how you're splitting it (or that you'll handle it directly if it's simple), and "
        "what the user will get at the end. This is how the user catches a misunderstanding "
        "before you burn work on the wrong thing.\n"
        "- NEVER send only the confirmation and stop — the message is a receipt for work "
        "already starting, not the answer. Skip confirmations entirely for pure conversation; "
        "they are for work, not chatter.\n\n"
        "Subagent Telegram updates must be minimal and concise: at most one optional progress "
        "update plus one completion update per subagent, never step-by-step internal logs. "
        "For action requests (open/click/search/screenshot/send), execute tools first and then report results. "
        "For send-email tasks, use email_execute/send_message (ask only for missing fields like subject/body) and do not open browser compose URLs. "
        "Do not return a plan-only final reply like 'let me' or 'I will'. "
        "For browser tasks, stay in the current tab unless the user explicitly asks for new tabs/windows.\n\n"
        "LONG TASKS: You are built to run for as long as a task needs. If you use your whole round "
        "budget while still making progress you will be granted continuations automatically — keep "
        "working, do not restart or summarize prematurely. To avoid drowning in your own output: "
        "re-run tools with narrower parameters rather than dumping huge outputs, and prefer writing "
        "big results to files. If the user sends a new message mid-task you will receive it between "
        "rounds — adjust course, do not queue it for later.\n\n"
        "CRITICAL — Answering vs. messaging:\n"
        "Your final plain-text response is already delivered to the user. send_telegram_message is ONLY for "
        "progress updates during long multi-step work — it is not how you answer.\n"
        "1. For a question you can answer directly, just write the answer as text. Call NO tools.\n"
        "2. Never send the same message twice. For anything longer than a few minutes of work, keep the "
        "user posted: one short update at each real milestone (or roughly every 10 minutes on long tasks) "
        "saying what is done and what is next. A short 'X finished, starting Y' beats silence — the user "
        "cannot see your tool calls.\n"
        "3. Never invent work that was not requested. If the user asked a question, answer it — "
        "do not create projects, files, or demos they did not ask for.\n"
        "4. When you have nothing new to do, stop calling tools and write your final answer.\n\n"
        "CRITICAL — Command Loop Prevention:\n"
        "Many shell commands (cp, mv, mkdir, chmod, ln, etc.) succeed SILENTLY with no output. "
        "An empty stdout with exit code 0 means SUCCESS, not failure. Do NOT re-run a command "
        "that already succeeded. If a tool result shows exit_code 0, move on immediately.\n"
        "If you already ran a command and got a result, do not run it again. Check the conversation "
        "history before calling any tool — if you already have the answer, use it."
    )

    if raid_mode:
        parts.append(_RAID_MODE_PROMPT)

    summary = db.get_summary(user_id)
    if summary:
        parts.append(f"[Previous conversation summary]\n{summary}")

    if include_memories and current_message:
        if relevant_memories is None:
            relevant_memories = memory.get_relevant_memories(user_id, current_message)
        if relevant_memories:
            memories_str = memory.format_memories_for_context(relevant_memories)
            parts.append(memories_str)

    return "\n\n".join(parts) if parts else None


async def _rerank_memories(user_id: int, message: str) -> list[dict] | None:
    """Rank keyword-matched memories with the decision layer.

    Returns the reordered top memories, or None to keep the default keyword
    ranking (decision layer off, unavailable, or it had nothing to say).
    """
    if not laya_decisions.feature_enabled("memory_rerank"):
        return None
    if not message.strip():
        return None
    candidates = memory.get_relevant_memories(user_id, message, limit=12)
    if len(candidates) <= 1:
        return None

    state = {
        "current_message": message[:2000],
        "candidate_memories": [
            {"n": i + 1, "memory": str(m.get("content") or "")[:300]}
            for i, m in enumerate(candidates)
        ],
    }
    questions = {
        f"relevant_{i + 1}": laya_decisions.noul(
            f"Is `candidate_memories[{i}].memory` useful context for answering `current_message`?"
        )
        for i in range(len(candidates))
    }
    result = await laya_decisions.try_decide(state, questions, feature="memory_rerank")
    if not result:
        return None

    answers = result.get("answers") or {}
    scored: list[dict] = []
    for i, m in enumerate(candidates):
        prob = (answers.get(f"relevant_{i + 1}") or {}).get("noul")
        if prob is None:
            continue
        m = dict(m)
        m["relevance"] = round(float(prob), 2)
        scored.append(m)
    if not scored:
        return None
    scored.sort(key=lambda m: m["relevance"] * max(1, int(m.get("importance") or 5)), reverse=True)
    return [m for m in scored if m["relevance"] >= 0.5][:5]


async def _triage_model(base_model: str, text: str) -> str | None:
    """Route clearly simple messages to the fast model. None keeps base_model.

    Runs once before the turn starts — TALOS never switches models mid-task.
    """
    if not laya_decisions.feature_enabled("model_triage"):
        return None
    fast_model = os.getenv("FAST_MODEL", "").strip()
    if not fast_model or fast_model == base_model:
        return None
    provider, _ = model_router.resolve_model(fast_model)
    if not model_router._provider_enabled(provider):
        return None
    if not await laya_decisions.available():
        return None

    result = await laya_decisions.try_decide(
        text[:4000],
        {
            "complexity": laya_decisions.choice(
                "How complex is this user request for an AI assistant?",
                {
                    "simple": "A greeting, quick factual question, small talk, or a one-step ask.",
                    "standard": "A normal request needing a thoughtful answer or a couple of steps.",
                    "complex": "Multi-step work, analysis, planning, or anything needing many tools.",
                },
            ),
            "needs_tools": laya_decisions.noul(
                "Answering this request well requires tools (terminal, browser, email, search) rather than a direct reply."
            ),
        },
        feature="model_triage",
    )
    if not result:
        return None

    answers = result.get("answers") or {}
    complexity = answers.get("complexity") or {}
    needs_tools = (answers.get("needs_tools") or {}).get("noul")
    confidence = complexity.get("confidence") or 0.0
    if (
        complexity.get("choice") == "simple"
        and confidence >= laya_decisions.confidence_min()
        and (needs_tools is not None and needs_tools < 0.5)
    ):
        logger.info(
            "Model triage: routing simple message to fast model %s (confidence %.2f)",
            fast_model,
            confidence,
        )
        return fast_model
    return None


def _build_subagent_system(user_id: int, role: str, task: str, context: str = "", raid_mode: bool = False) -> str | None:
    parts = []
    prompt = db.read_system_prompt()
    if prompt:
        parts.append(prompt)

    parts.append(f"[Current time] {_now_str()}")

    if raid_mode:
        # Raid mode: the orchestrator runs the show. Subagents stay silent and
        # report through their final result; only true blockers reach the user,
        # relayed by the orchestrator.
        parts.append(
            "You are a subagent delegated by the main TALOS orchestrator, currently running "
            "a coordinated swarm (RAID MODE). Stay narrowly focused on the assigned task and "
            "its scope boundaries. Do not spawn other subagents.\n\n"
            "IMPORTANT — REPORTING (raid rules):\n"
            "1. Do NOT send Telegram messages to the user. All reporting goes to the "
            "orchestrator through your FINAL result — make it complete, structured, and "
            "integration-ready: what you did, what you found, what is missing.\n"
            "2. The ONLY exception: if you hit a hard blocker that needs the user (missing "
            "credential, approval for a destructive action, ambiguous requirement), state it "
            "clearly in your result as 'NEEDS USER:' plus the question — the orchestrator "
            "will relay it.\n"
            "3. Never stream per-step logs or 'still working' updates. Work quietly, "
            "finish, report once.\n"
            "Do not run passive wait commands like sleep/timeout/checking-in loops.\n\n"
            "CRITICAL — Command Loop Prevention:\n"
            "Many shell commands (cp, mv, mkdir, chmod, ln, etc.) succeed SILENTLY with no output. "
            "An empty stdout with exit code 0 means SUCCESS. Do NOT re-run a command that succeeded. "
            "If you already ran a command and got a result, do not run it again. Move on."
        )
    else:
        parts.append(
            "You are a subagent delegated by the main TALOS orchestrator. "
            "Stay narrowly focused on the assigned task. Do not spawn other subagents.\n\n"
            "IMPORTANT: You have access to send_telegram_message for concise status updates only.\n"
            "Rules:\n"
            "1. Send at most one brief start update and one completion update\n"
            "2. For LONG tasks, send one short update at each real milestone (or roughly every 10 minutes) — "
            "the user cannot see your tool calls and needs to know you are alive and what is happening\n"
            "3. Never stream per-step logs, repeated narration, raw transcripts, or generic 'still working' spam\n"
            "4. Keep each update short (one paragraph, max ~240 chars)\n"
            "Write updates in clean plain English with complete sentences. Avoid random or unusual characters.\n"
            "Do not run passive wait commands like sleep/timeout/checking-in loops.\n"
            "Never end on a 'still working' update without later sending an explicit completion or failure update.\n"
            "Keep messages concise and useful. Sign off with your role in brackets so the user "
            "knows which subagent is talking, e.g. [researcher] or [executor].\n\n"
            "CRITICAL — Command Loop Prevention:\n"
            "Many shell commands (cp, mv, mkdir, chmod, ln, etc.) succeed SILENTLY with no output. "
            "An empty stdout with exit code 0 means SUCCESS. Do NOT re-run a command that succeeded. "
            "If you already ran a command and got a result, do not run it again. Move on."
        )
    parts.append(f"[Subagent role]\n{role or 'general'}")
    parts.append(f"[Delegated task]\n{task}")
    if context.strip():
        parts.append(f"[Delegation context]\n{context.strip()}")

    if _is_email_configured():
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


_MAX_AGENT_TELEGRAM_MESSAGES = int(os.getenv("MAX_AGENT_TELEGRAM_MESSAGES", "8"))
_MAX_IDENTICAL_TOOL_CALLS = int(os.getenv("MAX_IDENTICAL_TOOL_CALLS", "1"))
_MAX_STALLED_ROUNDS = int(os.getenv("MAX_STALLED_ROUNDS", "2"))
_MAX_EMPTY_ROUNDS = int(os.getenv("MAX_EMPTY_ROUNDS", "2"))

# Streamed model output is batched before hitting the activity feed so a fast
# model does not flood the SSE channel with one event per token.
_STREAM_FLUSH_CHARS = int(os.getenv("STREAM_FLUSH_CHARS", "80"))
_STREAM_FLUSH_INTERVAL_S = float(os.getenv("STREAM_FLUSH_INTERVAL_S", "0.35"))
_STREAM_MAX_EVENT_CHARS = 2000

# Tools that are safe (and often necessary) to repeat with identical arguments.
_REPEATABLE_TOOLS = {
    "read_file",
    "list_files",
    "list_projects",
    "list_tools",
    "get_memories",
    "subagent_status",
}


class AlreadySentReply(str):
    """A final reply the agent already delivered through a message tool.

    Behaves as a normal string everywhere, but lets `core.process_message`
    recognise text the user has already received so it is not sent twice.
    """

    __slots__ = ()


def was_already_sent(reply: object) -> bool:
    return isinstance(reply, AlreadySentReply)


# Tools whose only effect is delivering text to the user. A round made up
# solely of these means the model has answered rather than done more work.
_USER_FACING_MESSAGE_TOOLS = {"send_telegram_message", "send_voice_message"}

_HANDOFF_RE = re.compile(
    r"\b(i(?:'ll| will| am going to| can| shall)|let me|give me|hold on|"
    r"stand by|starting|getting started|working on|i'm on it)\b",
    re.IGNORECASE,
)


def _message_defers_work(text: str) -> bool:
    """True when a message promises future work rather than concluding the turn."""
    stripped = (text or "").strip()
    if not stripped:
        return True
    if _is_low_value_progress_message(stripped):
        return True
    if _looks_like_completion_message(stripped):
        return False
    # Only treat the opening as a promise, so a long answer that merely contains
    # "I will" partway through still counts as a real answer.
    return bool(_HANDOFF_RE.match(stripped)) or bool(_HANDOFF_RE.match(stripped[:40]))


def _tool_call_signature(tool_name: str, tool_args: dict) -> str:
    """Stable key identifying "this exact call again"."""
    try:
        args_repr = json.dumps(tool_args, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        args_repr = str(sorted(tool_args.items())) if isinstance(tool_args, dict) else str(tool_args)
    return f"{tool_name}:{args_repr}"


def _tool_result_is_failure(result_text: str) -> bool:
    """True when a tool result carries no new information for the model.

    A round in which every call came back like this is a round that made no
    progress, even though each call "ran". Rate limiting is the case that
    mattered in practice: the executor refuses, the model reads the refusal as a
    bad command, rewrites it slightly, and burns every remaining round without
    the repeat-guard ever matching.
    """
    text = str(result_text or "").strip()
    if not text:
        return True
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(parsed, dict):
        return False
    if parsed.get("rate_limited") or parsed.get("blocked") or parsed.get("error"):
        return True
    if parsed.get("ok") is False:
        return True
    return False


def _estimate_messages_chars(messages: list[dict]) -> int:
    """Rough content size of a message list, used for context budgeting."""
    total = 0
    for m in messages:
        total += len(str(m.get("content") or "")) + 64
        for tc in m.get("tool_calls") or []:
            if isinstance(tc, dict):
                total += len(str(tc.get("function", {}).get("arguments") or ""))
    return total


def _truncate_tool_result(result_text: str, cap: int | None = None) -> str:
    """Clamp a tool result before it enters the run's context.

    Long command output and file dumps are the usual context killers on
    multi-hour tasks. The model keeps the head (enough to act on) and is told
    how to get the rest if it actually needs it.
    """
    limit = _MAX_TOOL_RESULT_CHARS if cap is None else cap
    text = str(result_text or "")
    if len(text) <= limit:
        return text
    return (
        text[:limit]
        + f"\n\n[truncated at {limit} chars — the full output is longer. "
        "If you need more of it, re-run the tool with narrower parameters, or "
        "write it to a file and read the part you need with read_file offset/limit.]"
    )


async def _decide_compaction(targets: list[int], messages: list[dict], task_text: str) -> dict[int, str] | None:
    """One decision-model call: keep / truncate / drop for each old tool result.

    Returns {candidate index -> decision} for the confident answers, or None to
    use the deterministic default (truncate everything selected).
    """
    if not targets or len(targets) > 30:
        return None
    if not task_text.strip():
        return None
    if not laya_decisions.feature_enabled("context_compact"):
        return None
    if not await laya_decisions.available():
        return None

    candidates = [
        {"n": n + 1, "head": str(messages[i].get("content") or "")[:300]}
        for n, i in enumerate(targets)
    ]
    state = {"task": task_text[:2000], "old_tool_results": candidates}
    questions = {
        f"result_{n + 1}": laya_decisions.choice(
            f"Is `old_tool_results[{n}].head` still needed to finish `task`?",
            {
                "keep": "Still needed to finish the task; must stay verbatim.",
                "truncate": "Partly useful; keeping the beginning is enough.",
                "drop": "No longer relevant to finishing the task.",
            },
        )
        for n in range(len(candidates))
    }
    result = await laya_decisions.try_decide(state, questions, feature="context_compact")
    if not result:
        return None

    answers = result.get("answers") or {}
    decisions: dict[int, str] = {}
    for n in range(len(candidates)):
        answer = answers.get(f"result_{n + 1}") or {}
        decision = answer.get("choice")
        if decision in {"keep", "truncate", "drop"} and (answer.get("confidence") or 0.0) >= laya_decisions.confidence_min():
            decisions[n] = decision
    return decisions


async def _compact_run_context(messages: list[dict], keep_recent: int = 12, task_text: str = "") -> bool:
    """Shrink old tool results once a run's context grows past the budget.

    Default (no model call): the system prompt, user/assistant messages, and
    the most recent tool results are preserved verbatim; older tool results
    become short stubs. When the decision layer is available, it decides per
    result whether to keep it verbatim, truncate it, or drop it entirely, so
    important early findings survive long runs. Returns True when the size
    threshold was crossed and a compaction pass ran.
    """
    if _estimate_messages_chars(messages) <= _MAX_RUN_CONTEXT_CHARS:
        return False

    tool_indexes = [
        i for i, m in enumerate(messages)
        if m.get("role") == "tool" and len(str(m.get("content") or "")) > 600
    ]
    targets = tool_indexes[:-keep_recent] if keep_recent else tool_indexes

    decisions = await _decide_compaction(targets, messages, task_text) if targets else None

    for n, i in enumerate(targets):
        content = str(messages[i].get("content") or "")
        decision = (decisions or {}).get(n, "truncate")
        if decision == "keep":
            continue
        if decision == "drop":
            messages[i] = {
                **messages[i],
                "content": (
                    "[dropped by context compaction — judged no longer relevant; "
                    "re-run that tool if it turns out you need it]"
                ),
            }
        else:
            messages[i] = {
                **messages[i],
                "content": (
                    content[:300].rstrip()
                    + "\n...[older result compacted to save context — if you need it "
                    "again, re-run that tool or read the file it produced]"
                ),
            }
    return True


# Long-running subagents spawned with background=true live here until they
# finish. Results are pushed into the parent run's interrupt queue when the
# orchestrator is still alive, or sent straight to the user otherwise.
_BACKGROUND_SUBAGENTS: dict[str, dict] = {}


class _LoopGuard:
    """Stops a model from burning its rounds repeating itself.

    Small local models in particular tend to re-issue the same tool call every
    round — most visibly by sending the user the same Telegram message over and
    over. Instead of executing those, we hand the model an explicit refusal so it
    has a chance to change course, and abandon the loop if it will not.
    """

    def __init__(self, is_orchestrator: bool = True):
        self._call_counts: dict[str, int] = {}
        self._sent_messages: set[str] = set()
        self._telegram_sends = 0
        self._max_telegram = max(1, min(int(_MAX_AGENT_TELEGRAM_MESSAGES), 50)) if is_orchestrator else 10**6
        self._max_identical = max(1, min(int(_MAX_IDENTICAL_TOOL_CALLS), 10))
        self.stalled_rounds = 0

    def grant_messages(self, extra: int) -> None:
        """Raise the per-turn Telegram message budget.

        Long tasks earn a little more messaging headroom every time they
        continue past a round budget: a two-hour job that can only message
        the user a handful of times total feels silent, which is worse than
        an occasional extra progress line.
        """
        self._max_telegram = min(self._max_telegram + max(0, int(extra)), 60)

    def check(self, tool_name: str, tool_args: dict) -> str | None:
        """Return a blocking tool result, or None to let the call through."""
        if tool_name in {"send_telegram_message", "send_voice_message"}:
            text = str(tool_args.get("message") or tool_args.get("text") or "").strip().lower()
            normalized = " ".join(text.split())
            if normalized and normalized in self._sent_messages:
                return json.dumps({
                    "sent": False,
                    "blocked": "duplicate_message",
                    "instruction": (
                        "You already sent this exact message. Do not send it again. "
                        "Either do real work with another tool or write your final answer now."
                    ),
                })
            if self._telegram_sends >= self._max_telegram:
                return json.dumps({
                    "sent": False,
                    "blocked": "message_limit_reached",
                    "instruction": (
                        f"You have already sent {self._telegram_sends} messages this turn, which is the limit. "
                        "Stop sending updates and write your final answer as plain text instead."
                    ),
                })
            if normalized:
                self._sent_messages.add(normalized)
            self._telegram_sends += 1
            return None

        if tool_name in _REPEATABLE_TOOLS:
            return None

        signature = _tool_call_signature(tool_name, tool_args)
        self._call_counts[signature] = self._call_counts.get(signature, 0) + 1
        if self._call_counts[signature] > self._max_identical:
            return json.dumps({
                "error": "repeated_tool_call",
                "instruction": (
                    f"You have already called {tool_name} with these exact arguments "
                    f"{self._call_counts[signature] - 1} time(s) and have the result above. "
                    "Do not call it again. Use the result you already have, try a different "
                    "approach, or write your final answer."
                ),
            })
        return None

    def note_round(self, all_blocked: bool) -> bool:
        """Record a round's outcome. Returns True when the agent should give up."""
        if all_blocked:
            self.stalled_rounds += 1
        else:
            self.stalled_rounds = 0
        return self.stalled_rounds >= max(1, int(_MAX_STALLED_ROUNDS))


def _preview_tool_args(tool_args: dict) -> dict:
    """Short, scalar-only argument preview for the activity timeline row."""
    if not isinstance(tool_args, dict):
        return {}
    return {k: str(v)[:100] for k, v in tool_args.items() if isinstance(v, (str, int, float, bool))}


_MAX_ACTIVITY_ARG_CHARS = 4000


def _full_tool_args(tool_args: dict) -> dict:
    """Full arguments (including nested values) for the activity detail panel."""
    if not isinstance(tool_args, dict):
        return {}
    full = {}
    for key, value in tool_args.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            text = str(value)
        else:
            try:
                text = json.dumps(value, ensure_ascii=False)
            except (TypeError, ValueError):
                text = str(value)
        full[key] = text[:_MAX_ACTIVITY_ARG_CHARS]
    return full


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
    out_dir = app_paths.browser_artifacts_dir()
    os.makedirs(out_dir, exist_ok=True)
    return os.path.join(out_dir, f"{prefix}_{stamp}.png")


def _resolve_output_image_path(path: str | None, prefix: str) -> str:
    raw = str(path or "").strip()
    data_root = os.path.realpath(_DATA_DIR)
    if raw:
        resolved = raw if os.path.isabs(raw) else os.path.join(_DATA_DIR, raw)
        resolved = os.path.realpath(resolved)
        if not resolved.startswith(data_root + os.sep) and resolved != data_root:
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
    resolved = raw if os.path.isabs(raw) else os.path.join(_DATA_DIR, raw)
    resolved = os.path.realpath(resolved)
    roots = [os.path.realpath(_DATA_DIR), os.path.realpath(_RESOURCE_DIR)]
    if not any(resolved.startswith(root + os.sep) or resolved == root for root in roots):
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


def _subagent_message_budget(long_running: bool = False) -> tuple[int, float, int]:
    """(max_messages, min_interval_s, max_chars) for a subagent's user updates.

    A subagent that runs for hours must be able to keep the user posted at
    milestones; the short-task budget (a couple of updates) would leave them
    silent for most of the run.
    """
    if long_running:
        max_messages = max(8, min(int(_SUBAGENT_MAX_TELEGRAM_MESSAGES), 16))
        min_interval = 30.0
    else:
        max_messages = max(1, min(int(_SUBAGENT_MAX_TELEGRAM_MESSAGES), 6))
        min_interval = max(0.0, min(float(_SUBAGENT_MIN_UPDATE_INTERVAL_S), 120.0))
    max_chars = max(80, min(int(_SUBAGENT_MAX_TELEGRAM_MESSAGE_CHARS), 1000))
    return max_messages, min_interval, max_chars


def _build_subagent_send_func(send_func: SendFunc | None, long_running: bool = False) -> SendFunc | None:
    if send_func is None:
        return None

    max_messages, min_interval, max_chars = _subagent_message_budget(long_running)

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
                "name": "laya_decide",
                "description": (
                    "Ask the fast decision model typed questions about a text and get structured answers "
                    "with probabilities in one call — much cheaper and faster than a full model round. "
                    "Give it the text to judge in `state` and 1-10 focused questions. Each question needs: "
                    "type (choice|score|noul), instructions (the question), and criteria (choice: {option_id: description}; "
                    "score: ordered level descriptions; noul: none). Answers come back as your option ids / level numbers / "
                    "yes-probabilities with a confidence each. Use it to classify, route, verify, or gate your own steps; "
                    "do not use it to generate text or reason through multi-step plans."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "state": {
                            "type": "string",
                            "description": "The text to judge: an email, ticket, message, document, or JSON-serialized record"
                        },
                        "questions": {
                            "type": "object",
                            "description": (
                                "Map of question id -> question object. Each object: "
                                "{type: 'choice', instructions: 'which team should handle this?', criteria: {billing: 'payment issues', technical: 'bugs and outages'}} "
                                "or {type: 'noul', instructions: 'does the sender request a refund?'} "
                                "or {type: 'score', instructions: 'how urgent is this?', criteria: ['not urgent', 'soon', 'blocking']}"
                            ),
                            "additionalProperties": {"type": "object"}
                        }
                    },
                    "required": ["state", "questions"]
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
                    "Supports: calendar.list_events, calendar.create_event, calendar.get_event, calendar.update_event, "
                    "calendar.delete_event, calendar.list_calendars, "
                    "drive.list_files, drive.get_file, drive.export_file, sheets.get_values, sheets.append_row. "
                    "If GOOGLE_APPS_SCRIPT_URL is configured, any custom action is forwarded to the Apps Script."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "Action name, e.g. 'calendar.list_events', 'calendar.create_event', 'calendar.delete_event', 'drive.list_files', 'sheets.get_values'"},
                        "payload": {"type": "object", "description": "Action payload object"}
                    },
                    "required": ["action"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "homeassistant_execute",
                "description": (
                    "Control and read the user's Home Assistant smart home. Actions: "
                    "status (connection check), list_entities (compact list of all sensors/devices, "
                    "filterable by domain e.g. 'light' or by free-text search), get_state (full state "
                    "of one entity), call_service (control a device, e.g. light/turn_on, climate/set_temperature, "
                    "cover/open_cover, media_player/play_media, automation/trigger). "
                    "Always discover the exact entity_id with list_entities first."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["status", "list_entities", "get_state", "call_service"],
                            "description": "What to do",
                        },
                        "domain": {"type": "string", "description": "list_entities: filter by domain (light, switch, sensor, climate, cover, ...). call_service: service domain, e.g. 'light'"},
                        "search": {"type": "string", "description": "list_entities: free-text filter matched against entity_id and friendly name"},
                        "entity_id": {"type": "string", "description": "get_state / call_service: the entity, e.g. 'light.kitchen'"},
                        "service": {"type": "string", "description": "call_service: service name, e.g. 'turn_on', 'toggle', 'set_temperature'"},
                        "data": {"type": "object", "description": "call_service: extra service data, e.g. {\"brightness\": 200, \"temperature\": 21}"}
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
                                "thread_message, triage_messages, send_message, download_attachments, extract_message, "
                                "export_message, reply_message, forward_message, move_messages, copy_messages, delete_messages. "
                                "triage_messages classifies inbox messages (urgent / needs_reply / spam / phishing / newsletter) "
                                "with the fast decision model and returns a ready-to-read digest."
                            ),
                        },
                        "account": {"type": "string", "description": "Optional Himalaya account name"},
                        "folder": {"type": "string", "description": "Optional source folder (defaults to inbox)"},
                        "page": {"type": "number", "description": "Optional page number for list_messages"},
                        "limit": {"type": "number", "description": "Max messages to triage (triage_messages, default 20)"},
                        "move_spam_to": {"type": "string", "description": "Folder to move confident spam/phishing into (triage_messages, optional)"},
                        "message_id": {
                            "description": "Single message/envelope id for read/reply/forward/thread",
                            "oneOf": [{"type": "number"}, {"type": "string"}],
                        },
                        "message_ids": {
                            "type": "array",
                            "items": {
                                "oneOf": [{"type": "number"}, {"type": "string"}],
                            },
                            "description": "List of ids for bulk actions (move/copy/delete)",
                        },
                        "target_folder": {"type": "string", "description": "Target folder for move/copy"},
                        "preview": {"type": "boolean", "description": "Read/thread in preview mode (avoid marking seen). Default true."},
                        "output_path": {
                            "type": "string",
                            "description": "Optional file path for read_message/thread_message output. Relative paths are saved under data/email_exports.",
                        },
                        "download_dir": {
                            "type": "string",
                            "description": "Optional directory for download_attachments. Relative paths are saved under data/email_exports.",
                        },
                        "destination_path": {
                            "type": "string",
                            "description": "Optional destination path for extract_message/export_message. For extract_message, prefer a directory.",
                        },
                        "full": {
                            "type": "boolean",
                            "description": "For extract_message/export_message: true exports a single raw .eml message, false exports MIME parts.",
                        },
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
                        "attachments": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional file paths to attach for send_message. Non-absolute paths are resolved against workspace/data roots.",
                        },
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
                "name": "pa_system",
                "description": (
                    "Speak a message out loud through the host machine speakers (server-side). "
                    "Use this for local PA announcements. This does NOT use browser audio."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Message to announce out loud"},
                        "repeat": {"type": "number", "description": "How many times to repeat (default: 1, max: 5)"},
                        "backend": {
                            "type": "string",
                            "description": "TTS backend: auto, windows_sapi, say, spd_say, espeak"
                        },
                        "voice": {"type": "string", "description": "Optional voice name for supported backends"},
                        "rate": {
                            "type": "number",
                            "description": "Optional speaking rate. Range depends on backend."
                        }
                    },
                    "required": ["text"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "panel_popup",
                "description": (
                    "Push a popup to the user's open Panel browser tabs (the always-on tablet dashboard). "
                    "Use action 'confirm' for yes/no decisions — the call waits until the user answers "
                    "(or times out) and returns the answer. Use action 'show_html' to display temporary "
                    "custom HTML (diagrams, tables, sketches) on the Panel until dismissed or expired."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": "confirm (wait for a yes/no answer), show_html (temporary HTML card), dismiss (close a popup by id)",
                        },
                        "title": {"type": "string", "description": "Popup heading shown to the user"},
                        "question": {"type": "string", "description": "confirm: the yes/no question shown to the user"},
                        "timeout_s": {"type": "number", "description": "confirm: max seconds to wait for an answer (default 90, max 300)"},
                        "yes_label": {"type": "string", "description": "confirm: custom text for the yes button (default Yes)"},
                        "no_label": {"type": "string", "description": "confirm: custom text for the no button (default No)"},
                        "html": {"type": "string", "description": "show_html: self-contained HTML fragment (inline CSS/SVG/JS only, max 20000 chars)"},
                        "ttl_s": {"type": "number", "description": "show_html: seconds before the popup disappears (default 300, max 3600)"},
                        "text": {"type": "string", "description": "show_html: optional plain-text note shown above the HTML"},
                        "popup_id": {"type": "string", "description": "dismiss: id of the popup to close"},
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
                                        "description": "Action: goto, click, type, press, wait_for, extract, eval, select, scroll, screenshot, assert, set_page, new_tab, close_tab"
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
                                    "page_index": {"type": "number"},
                                    "expression": {"type": "string"},
                                    "arg": {"type": "string"}
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
                "name": "browser_auto",
                "description": (
                    "Experimental two-tier browser agent: a fast decision model makes each move (click, type, select, "
                    "arrow keys, scroll) from a numbered list of page elements, and it escalates to YOU when it cannot "
                    "confidently continue. It returns status 'completed' only after an independent verification pass "
                    "against the goal. When it stops early, read the 'escalation' block: "
                    "low_confidence → look at browser_state and take over or retry with a clearer goal; "
                    "blocked → the page has a login wall/captcha/dialog, handle it with browser_run or ask the user; "
                    "needs_text → write the text for the reported field and call browser_auto again with type_texts; "
                    "verification_failed → the loop claimed done but the final page disagrees, check browser_state; "
                    "stuck → same action repeated, take over. Cookie/consent banners are dismissed automatically. "
                    "Canvas games are detected and played blind via arrow keys with the score text as feedback. "
                    "It never types text of its own — only strings quoted in the goal or passed via type_texts. "
                    "Use browser_run for exact step-by-step control or guaranteed action sequences."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "goal": {
                            "type": "string",
                            "description": "What to achieve on the page. Quote any text to type, e.g. search for \"best laptops\""
                        },
                        "max_steps": {"type": "number", "description": "Maximum steps before stopping (default: 25, cap 50)"},
                        "type_texts": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional list of strings the loop may type (default: strings quoted in the goal)",
                        },
                        "screenshot_every": {
                            "type": "number",
                            "description": "Capture a screenshot every N steps and return their paths (e.g. 4 for every 4 moves in a game)",
                        },
                        "auto_connect": {"type": "boolean", "description": "Auto-connect/start browser when no session exists (default: true)"},
                        "default_timeout_ms": {"type": "number", "description": "Per-step timeout in ms (default: 15000)"}
                    },
                    "required": ["goal"]
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
                                    "values": {
                                        "type": "array",
                                        "description": "Row values (mixed types allowed)",
                                        "items": {"anyOf": [{"type": "string"}, {"type": "number"}, {"type": "boolean"}]},
                                    }
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
                    "Copy an existing HTML file or directory on disk into the project gateway and make it live. "
                    "If source_path is a directory, all files are copied recursively. "
                    "If it's a single HTML file, it becomes index.html in the project. "
                    "No need to read files first — just pass the source path and a project name. "
                    "Returns the full public URL. You MUST send the returned url to the user."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Project name (alphanumeric, hyphens, underscores). Used in the URL."},
                        "source_path": {"type": "string", "description": "Path to an HTML file or a directory to copy into the project"},
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
                    "Delegate a task to a subagent that can message the user directly on "
                    "Telegram. Multiple spawn_subagent calls in one response run in PARALLEL — "
                    "this is the fast path: give each independent part of a request its own "
                    "subagent in the SAME response instead of handling parts sequentially. "
                    "Use for research, planning, review, data gathering, testing, or execution "
                    "subtasks — anything focused that would otherwise occupy your rounds. "
                    "Use background=true for LONG-RUNNING subtasks (over ~15 minutes): the subagent "
                    "keeps working while you continue other work (up to 8 at once), its result is "
                    "delivered to you when it finishes, and you can check on it with subagent_status."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "role": {"type": "string", "description": "Short role label: researcher, planner, reviewer, executor, etc."},
                        "task": {"type": "string", "description": "The specific task to delegate"},
                        "context": {"type": "string", "description": "Extra context or constraints"},
                        "background": {"type": "boolean", "description": "Run detached: return immediately and get the result later (default false). Use for long tasks."},
                        "timeout_s": {"type": "number", "description": "Idle timeout in seconds for this subagent (default from settings, max 86400). Long-running subagents should set e.g. 3600."}
                    },
                    "required": ["task"]
                }
            }
        })
        tools.append({
            "type": "function",
            "function": {
                "name": "subagent_status",
                "description": (
                    "Check on background subagents spawned with background=true. Returns each "
                    "one's state (running/done/failed/timeout) and result summary."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "subagent_id": {"type": "string", "description": "Optional: only report this subagent"}
                    }
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
                "name": "initiate_voice_call",
                "description": (
                    "Initiate a proactive voice call to the user via the web app. "
                    "Use when you need user input during a task, want to clarify requirements, "
                    "or need to discuss something complex. The user will receive an incoming call notification."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "username": {"type": "string", "description": "The username of the user to call"},
                        "message": {"type": "string", "description": "The message to display with the incoming call (e.g., 'I need your input on the deployment strategy')"}
                    },
                    "required": ["username"]
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
# On-demand tool loading
# ---------------------------------------------------------------------------
#
# The full tool set is ~7k tokens of JSON schema on every request. A hosted
# model swallows that without noticing; a local model on CPU spends minutes
# prefilling it before it can write a single token, on every message. So local
# models start with a small core set plus `load_tools`, and pull in the schemas
# for a category only once they decide they need it. The system prompt already
# lists what exists, so the model knows what it can ask for.

_TOOL_CATEGORIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "email": ("read, search, send and reply to email", ("email_execute",)),
    "browser": (
        "drive a real browser, scrape pages",
        ("browser_start_chrome_debug", "browser_connect", "browser_run", "browser_auto",
         "browser_state", "browser_disconnect", "scrape_url"),
    ),
    "documents": ("spreadsheets and Word documents", ("spreadsheet_execute", "docx_execute")),
    "google": ("Google Workspace (Drive, Calendar, Sheets, ...)", ("google_execute",)),
    "projects": ("create and manage web projects", ("create_project", "migrate_project", "list_projects")),
    "scheduling": ("recurring/scheduled jobs", ("schedule_cron", "list_cron", "remove_cron")),
    "memory": (
        "remember and recall facts about the user",
        ("save_memory", "search_memories", "list_memories", "delete_memory", "update_memory"),
    ),
    "attachments": (
        "send photos, files and screenshots to the user",
        ("send_telegram_photo", "send_telegram_document", "send_telegram_screenshot"),
    ),
    "custom_tools": ("build and manage your own tools", ("create_tool", "delete_tool", "list_dynamic_tools")),
    "assistant": ("PA system and model preferences", ("pa_system", "set_model_prefs")),
    "smart_home": ("control the user's Home Assistant smart home", ("homeassistant_execute",)),
}

_LOAD_TOOLS_NAME = "load_tools"

_LAZY_TOOLS_NOTE = (
    "[Tool loading]\n"
    "Only a core set of tools is loaded right now: running commands, reading and writing "
    "files, web search, and messaging the user. Everything else listed above — "
    + ", ".join(_TOOL_CATEGORIES)
    + " — has to be loaded before you can call it: call load_tools with the categories you "
    "need, then call the tool itself in the same turn. Never tell the user a capability is "
    "unavailable just because its tool is not loaded yet."
)


def _category_of_tool(tool_name: str, dynamic_names: set[str]) -> str | None:
    """The category a tool is deferred behind, or None if it is always loaded."""
    for category, (_, names) in _TOOL_CATEGORIES.items():
        if tool_name in names:
            return category
    if tool_name in dynamic_names:
        return "custom_tools"
    return None


def _load_tools_definition() -> dict:
    menu = "; ".join(f"{name} ({desc})" for name, (desc, _) in _TOOL_CATEGORIES.items())
    return {
        "type": "function",
        "function": {
            "name": _LOAD_TOOLS_NAME,
            "description": (
                "Load the tools you need for this task. They become available immediately, "
                "in the same turn. Categories: " + menu + "."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "categories": {
                        "type": "array",
                        "items": {"type": "string", "enum": sorted(_TOOL_CATEGORIES)},
                        "description": "Categories to load",
                    }
                },
                "required": ["categories"],
            },
        },
    }


def _visible_tools(all_tools: list[dict], loaded: set[str]) -> list[dict]:
    """`all_tools` minus the categories that have not been asked for yet."""
    dynamic_names = {
        d.get("function", {}).get("name", "")
        for d in dynamic_tools.get_tool_definitions()
    }
    visible = []
    for tool in all_tools:
        name = tool.get("function", {}).get("name", "")
        category = _category_of_tool(name, dynamic_names)
        if category is None or category in loaded:
            visible.append(tool)
    visible.append(_load_tools_definition())
    return visible


def _lazy_tools_enabled(model: str) -> bool:
    """On for local models, where prompt size costs real time. Overridable."""
    raw = os.getenv("TALOS_LAZY_TOOLS", "").strip().lower()
    if raw in {"1", "true", "on", "yes"}:
        return True
    if raw in {"0", "false", "off", "no"}:
        return False
    provider, _ = model_router.resolve_model(model)
    return provider == "ollama"


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
    _interrupt_queue: asyncio.Queue | None = None,
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
            _tool_detail = f"Running: {command[:120]}"
            await _t.emit("command", _agent_id, "Command", _tool_detail, _with_activity_meta({"command": command}))
        elif tool_name == "spawn_subagent":
            pass
        else:
            _tool_detail = f"Calling {tool_name}"
            await _t.emit("tool", _agent_id, "Tool call", _tool_detail, _with_activity_meta({
                "tool": tool_name,
                "tool_args": _preview_tool_args(tool_args),
                "tool_args_full": _full_tool_args(tool_args),
            }))

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

        elif tool_name == "laya_decide":
            state = tool_args.get("state")
            questions = tool_args.get("questions")
            if state is None or not str(state).strip():
                return json.dumps({"error": "state is required: the text to judge"}, indent=2)
            if not isinstance(questions, dict) or not questions:
                return json.dumps({"error": "questions is required: a map of question id -> typed question"}, indent=2)
            try:
                result = await laya_decisions.decide(state, questions)
            except laya_decisions.LayaError as exc:
                return json.dumps({"error": str(exc)}, indent=2)
            answers = {
                qid: {
                    kk: vv for kk, vv in answer.items()
                    if kk in ("choice", "score", "noul", "confidence", "probabilities")
                }
                for qid, answer in result["answers"].items()
            }
            return json.dumps(
                {
                    "ok": True,
                    "provider": result["provider"],
                    "latency_ms": result["latency_ms"],
                    "answers": answers,
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

        elif tool_name == "homeassistant_execute":
            action = str(tool_args.get("action", "")).strip().lower()
            if not action:
                return json.dumps({"error": "action is required"}, indent=2)
            if action == "status":
                return json.dumps(await homeassistant.get_status(), indent=2)
            if action == "list_entities":
                result = await homeassistant.list_entities_compact(
                    domain=str(tool_args.get("domain", "") or ""),
                    search=str(tool_args.get("search", "") or ""),
                )
                return json.dumps(result, indent=2)
            if action == "get_state":
                entity_id = str(tool_args.get("entity_id", "") or "").strip()
                if not entity_id:
                    return json.dumps({"error": "entity_id is required for get_state"}, indent=2)
                return json.dumps(await homeassistant.get_entity_state(entity_id), indent=2)
            if action == "call_service":
                domain = str(tool_args.get("domain", "") or "").strip()
                service = str(tool_args.get("service", "") or "").strip()
                entity_id = str(tool_args.get("entity_id", "") or "").strip()
                if not domain or not service or not entity_id:
                    return json.dumps({"error": "domain, service, and entity_id are required for call_service"}, indent=2)
                data = tool_args.get("data")
                result = await homeassistant.call_service(domain, service, entity_id, data if isinstance(data, dict) else None)
                if result.get("ok"):
                    result["note"] = "Command sent to Home Assistant. Use get_state to verify the result if it matters."
                return json.dumps(result, indent=2)
            return json.dumps({"error": f"Unknown action: {action}. Use status, list_entities, get_state, or call_service."}, indent=2)

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
                output_path=tool_args.get("output_path"),
                download_dir=tool_args.get("download_dir"),
                destination_path=tool_args.get("destination_path"),
                full=tool_args.get("full"),
                reply_all=tool_args.get("reply_all"),
                to=tool_args.get("to"),
                cc=tool_args.get("cc"),
                bcc=tool_args.get("bcc"),
                subject=tool_args.get("subject"),
                body=tool_args.get("body"),
                attachments=tool_args.get("attachments"),
                headers=tool_args.get("headers"),
            )
            return json.dumps(result, indent=2)

        elif tool_name == "pa_system":
            text = str(tool_args.get("text", "")).strip()
            if not text:
                return json.dumps({"error": "text is required"}, indent=2)

            result = pa_system.announce(
                text=text,
                repeat=tool_args.get("repeat", 1),
                backend=str(tool_args.get("backend", "auto")).strip() or "auto",
                voice=str(tool_args.get("voice", "")).strip(),
                rate=tool_args.get("rate"),
            )
            return json.dumps(result, indent=2)

        elif tool_name == "panel_popup":
            action = str(tool_args.get("action", "")).strip().lower()
            if not action:
                return json.dumps({"error": "action is required: confirm, show_html, or dismiss"}, indent=2)

            if action == "confirm":
                question = str(tool_args.get("question", "")).strip()
                if not question:
                    return json.dumps({"error": "question is required for confirm"}, indent=2)
                timeout_s = tool_args.get("timeout_s", 90)
                if not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
                    timeout_s = 90
                timeout_s = int(min(timeout_s, panel_popups.MAX_CONFIRM_TIMEOUT_S))
                popup = panel_popups.hub.create_confirm(
                    title=str(tool_args.get("title", "")).strip() or "Decision needed",
                    text=question,
                    timeout_s=timeout_s,
                    yes_label=str(tool_args.get("yes_label", "")).strip() or "Yes",
                    no_label=str(tool_args.get("no_label", "")).strip() or "No",
                )
                result = await panel_popups.hub.wait_for_answer(popup["id"], timeout_s + 5)
                if result.get("status") == "answered":
                    answer = "yes" if result.get("answer") == "yes" else "no"
                    return json.dumps(
                        {
                            "status": "answered",
                            "answer": answer,
                            "note": "The user answered on the Panel.",
                        },
                        indent=2,
                    )
                return json.dumps(
                    {
                        "status": result.get("status") or "timeout",
                        "answer": None,
                        "note": "Nobody answered on the Panel in time. Ask the user in chat instead of retrying.",
                    },
                    indent=2,
                )

            if action == "show_html":
                created = panel_popups.hub.show_html(
                    title=str(tool_args.get("title", "")).strip() or "From your assistant",
                    html=tool_args.get("html", ""),
                    ttl_s=tool_args.get("ttl_s", 300),
                    text=str(tool_args.get("text", "")).strip(),
                )
                if "error" in created:
                    return json.dumps(created, indent=2)
                return json.dumps(
                    {
                        "status": "shown",
                        "popup_id": created["id"],
                        "ttl_s": created.get("ttl_s"),
                        "note": "Displayed on all open Panel pages; it disappears when dismissed or after the TTL.",
                    },
                    indent=2,
                )

            if action == "dismiss":
                popup_id = str(tool_args.get("popup_id", "")).strip()
                if not popup_id:
                    return json.dumps({"error": "popup_id is required for dismiss"}, indent=2)
                return json.dumps(panel_popups.hub.dismiss(popup_id), indent=2)

            return json.dumps({"error": f"Unknown action: {action}. Use confirm, show_html, or dismiss."}, indent=2)

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

        elif tool_name == "browser_auto":
            goal = str(tool_args.get("goal", "")).strip()
            if not goal:
                return json.dumps({"error": "goal is required: describe what to achieve on the page."}, indent=2)
            type_texts = tool_args.get("type_texts")
            if type_texts is not None and not isinstance(type_texts, list):
                return json.dumps({"error": "type_texts must be a list of strings."}, indent=2)
            result = await browser_automation.browser_auto(
                goal=goal,
                max_steps=tool_args.get("max_steps", 25),
                type_texts=type_texts,
                default_timeout_ms=tool_args.get("default_timeout_ms", 15000),
                auto_connect=tool_args.get("auto_connect"),
                screenshot_every=tool_args.get("screenshot_every", 0),
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
            result = {
                "status": "live",
                "url": reg["url"],
                "share_this_link": reg["url"],
                "path": reg["path"],
                "instruction": "Send the url to the user — this is the live clickable link to their project",
            }
            # Conductor doctrine nudge: the deck itself should be BUILT by a
            # subagent. Never blocks — just reminds the orchestrator to push
            # deliverable production down next time.
            if _agent_id == "orchestrator" and allow_subagent:
                result["reminder"] = (
                    "You built this yourself. In conductor mode, producing deliverables "
                    "(decks, sites, documents) belongs to a subagent — next time spawn a "
                    "deck-builder with the requirements and verify its result instead."
                )
            return json.dumps(result, indent=2)

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
            if not os.path.exists(source_path):
                return json.dumps({"error": f"Source not found: {source_path}"}, indent=2)
            description = str(tool_args.get("description", "")).strip()
            reg = gateway.register_project(name, description=description)
            dest_dir = reg["path"]
            os.makedirs(dest_dir, exist_ok=True)
            try:
                if os.path.isdir(source_path):
                    for item in os.listdir(source_path):
                        s = os.path.join(source_path, item)
                        d = os.path.join(dest_dir, item)
                        if os.path.isdir(s):
                            shutil.copytree(s, d, dirs_exist_ok=True)
                        else:
                            shutil.copy2(s, d)
                    file_count = sum(len(files) for _, _, files in os.walk(dest_dir))
                else:
                    dest_path = os.path.join(dest_dir, "index.html")
                    shutil.copy2(source_path, dest_path)
                    file_count = 1
            except Exception as e:
                return json.dumps({"error": f"Failed to copy: {e}"}, indent=2)
            return json.dumps({
                "status": "live",
                "url": reg["url"],
                "share_this_link": reg["url"],
                "path": reg["path"],
                "files_copied": file_count,
                "source": source_path,
                "instruction": "Send the url to the user — this is the live clickable link to their project",
            }, indent=2)

        elif tool_name == "list_projects":
            return json.dumps({"projects": gateway.list_projects()}, indent=2)

        elif tool_name == "send_telegram_message":
            message = _sanitize_response(str(tool_args.get("message", "")).strip())
            if not message:
                return json.dumps({"error": "No message provided"})
            if _is_low_value_progress_message(message):
                return json.dumps({"sent": False, "skipped": "low_value_progress"})
            if send_func:
                await _send_via_send_func(send_func, message)
                # Spell out what this is: a bare {"sent": true} gets misread by
                # weaker models as a new user message, which sends them looking
                # for more work to do.
                return json.dumps({
                    "tool": "send_telegram_message",
                    "sent": True,
                    "note": "This is tool output, not a message from the user. Your text was delivered.",
                    "instruction": "Do not send it again. If it answered the user, stop calling tools and write your final answer.",
                })
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

        elif tool_name == "initiate_voice_call":
            # Initiate a proactive voice call to the user
            username = str(tool_args.get("username", "")).strip()
            message = str(tool_args.get("message", "I need your input on something. Can we talk?")).strip()
            if not username:
                return json.dumps({"error": "No username provided"})

            # Try to initiate web call
            try:
                import webrtc_audio
                success = await webrtc_audio.initiate_proactive_web_call(username, message)
                if success:
                    return json.dumps({"sent": True, "method": "web"})
                else:
                    return json.dumps({"sent": False, "error": "User not connected"})
            except Exception as e:
                return json.dumps({"error": f"Failed to initiate call: {e}"})

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

            background_raw = tool_args.get("background", False)
            if isinstance(background_raw, bool):
                background = background_raw
            else:
                background = str(background_raw).strip().lower() in {"1", "true", "yes", "y", "on"}

            timeout_raw = tool_args.get("timeout_s")
            try:
                timeout_s = int(timeout_raw) if timeout_raw is not None else None
            except (TypeError, ValueError):
                timeout_s = None
            if timeout_s is not None:
                timeout_s = max(30, min(timeout_s, 86400))

            subagent_id = f"sub-{role}-{uuid.uuid4().hex[:6]}"
            await _t.emit(
                "spawn",
                _agent_id,
                "Spawning subagent",
                f"{role}: {task[:100]}",
                _with_activity_meta({"role": role, "task": task, "subagent_id": subagent_id, "parent_agent": _agent_id, "background": background}),
            )
            wall_timeout = timeout_s if timeout_s is not None else max(30, min(int(_MAX_SUBAGENT_WALL_TIMEOUT_S), 86400))
            sub_activity = {"start": time.monotonic(), "last": time.monotonic()}
            entry = {
                "id": subagent_id,
                "role": role,
                "task": task,
                "status": "running",
                "started_at": time.time(),
                "result": None,
                "error": None,
                "parent_agent": _agent_id,
                "user_id": user_id,
            }

            async def _run_and_record() -> str:
                try:
                    result = await _wait_for_agent(
                        _run_subagent(
                            user_id,
                            role,
                            task,
                            ctx,
                            _build_subagent_send_func(send_func, long_running=wall_timeout >= 1800),
                            _agent_id=subagent_id,
                            _parent_agent_id=_agent_id,
                            activity_state=sub_activity,
                        ),
                        wall_timeout,
                        sub_activity,
                    )
                    entry["status"] = "done"
                    entry["result"] = result
                    await _t.emit(
                        "done",
                        subagent_id,
                        "Subagent finished",
                        f"{role}: {task[:80]}",
                        _with_activity_meta({"duration_ms": round((time.monotonic() - _tool_t0) * 1000), "role": role, "subagent_id": subagent_id, "parent_agent": _agent_id}),
                    )
                except asyncio.TimeoutError:
                    entry["status"] = "timeout"
                    entry["error"] = f"Subagent idle timeout after {wall_timeout}s."
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
                                f"Subagent timed out after {wall_timeout}s while working on '{task[:120]}'. "
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
                except asyncio.CancelledError:
                    entry["status"] = "cancelled"
                    raise
                except Exception as exc:
                    entry["status"] = "failed"
                    entry["error"] = str(exc)
                    logger.exception(f"Background subagent {subagent_id} crashed")
                    return json.dumps({"error": f"Subagent failed: {exc}", "role": role, "task": task}, indent=2)
                return result

            if background:
                # Long-running detached subagent: return immediately, keep
                # working elsewhere. When it finishes, the result is pushed
                # into this run's interrupt queue (delivered between rounds)
                # and, if the parent run already ended, straight to the user.
                running = [e for e in _BACKGROUND_SUBAGENTS.values() if e.get("status") == "running"]
                # Raid mode is a live global toggle: read it here rather than
                # threading a parameter through every call site. (This gate
                # used to reference an out-of-scope `raid_mode` name and
                # crashed every background spawn.)
                cap = background_subagent_cap(db.get_raid_mode(user_id))
                if len(running) >= cap:
                    return json.dumps(
                        {
                            "error": "too_many_background_subagents",
                            "limit": cap,
                            "running": [e["id"] for e in running],
                            "instruction": "Wait for one to finish (check subagent_status) or run this task in the foreground.",
                        },
                        indent=2,
                    )
                # Same dict object _run_and_record mutates, so status/result
                # updates are visible through the registry.
                _BACKGROUND_SUBAGENTS[subagent_id] = entry

                async def _background_runner() -> None:
                    result_json = await _run_and_record()
                    payload = {
                        "text": (
                            f"Background subagent [{role}] finished task: {task[:200]}\n"
                            f"Result: {str(entry.get('result') or result_json)[:3000]}"
                        ),
                        "source": "subagent",
                    }
                    if interrupt_hub.queue_is_live(_interrupt_queue):
                        try:
                            _interrupt_queue.put_nowait(payload)
                        except asyncio.QueueFull:
                            pass
                    elif send_func is not None and entry.get("status") == "done":
                        # Parent run already ended; make sure the user sees it.
                        try:
                            await _send_via_send_func(
                                send_func,
                                f"Background subagent finished.\n\nTask: {task[:300]}\n\nResult: {str(entry.get('result'))[:3000]}\n[{role}]",
                            )
                        except Exception:
                            pass
                    # Opportunistic cleanup: keep the registry bounded.
                    finished = [
                        k for k, e in _BACKGROUND_SUBAGENTS.items()
                        if e.get("status") in {"done", "failed", "timeout", "cancelled"}
                        and time.time() - float(e.get("started_at") or 0) > 3600
                    ]
                    for k in finished[:20]:
                        _BACKGROUND_SUBAGENTS.pop(k, None)

                # Keep the task handle: an emergency stop (/stop, panel button)
                # must be able to cancel a running swarm, not just watch it.
                entry["task"] = asyncio.create_task(_background_runner())
                return json.dumps(
                    {
                        "ok": True,
                        "subagent_id": subagent_id,
                        "background": True,
                        "status": "running",
                        "timeout_s": wall_timeout,
                        "instruction": (
                            "The subagent is now running detached and will keep the user updated on "
                            "Telegram. Continue other work. Its final result will be delivered to you "
                            "automatically when it finishes; you can also call subagent_status to check."
                        ),
                    },
                    indent=2,
                )

            result = await _run_and_record()
            return json.dumps({"role": role, "task": task, "result": result}, indent=2)

        elif tool_name == "subagent_status":
            wanted = str(tool_args.get("subagent_id", "") or "").strip()
            entries = [
                e for e in _BACKGROUND_SUBAGENTS.values()
                if not wanted or e.get("id") == wanted
            ]
            if not entries:
                return json.dumps({"ok": True, "subagents": [], "note": "No background subagents on record. Foreground subagents are not tracked here."}, indent=2)
            running = sum(1 for e in entries if e.get("status") == "running")
            report = []
            for e in sorted(entries, key=lambda x: float(x.get("started_at") or 0)):
                item = {
                    "subagent_id": e["id"],
                    "role": e.get("role", ""),
                    "task": str(e.get("task", ""))[:200],
                    "status": e.get("status", "unknown"),
                    "elapsed_s": int(time.time() - float(e.get("started_at") or 0)),
                }
                if e.get("status") == "running":
                    report.append(item)
                    continue
                summary = str(e.get("result") or e.get("error") or "")
                item["summary"] = summary[:1500]
                report.append(item)
            return json.dumps({"ok": True, "running": running, "subagents": report}, indent=2)

        elif tool_name == "read_tool_guide":
            name = str(tool_args.get("tool_name", "")).strip()
            if not name:
                return json.dumps({"error": "tool_name is required"}, indent=2)
            filepath = ""
            for docs_dir in [_TOOLS_DIR, _DYNAMIC_TOOLS_DIR]:
                candidate = os.path.join(docs_dir, f"{name}.md")
                if os.path.isfile(candidate):
                    filepath = candidate
                    break
            if not filepath:
                available = set()
                for docs_dir in [_TOOLS_DIR, _DYNAMIC_TOOLS_DIR]:
                    if os.path.isdir(docs_dir):
                        available.update(f[:-3] for f in os.listdir(docs_dir) if f.endswith(".md"))
                return json.dumps({"error": f"Unknown tool: {name}", "available_tools": sorted(available)}, indent=2)
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
    r"<toolcall\b([^>]*)>(.+?)(?:</toolcall>|$)",
    re.DOTALL | re.IGNORECASE,
)
_TOOLCALL_ARG_RE = re.compile(
    r'\$(\w+)=("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|\[[^\]]*\]|\{[^}]*\}|[^\s<]+)',
)
_TOOLCALL_NAME_RE = re.compile(r"^([a-zA-Z_][\w:]*)")
_TOOLCALL_NAME_ATTR_RE = re.compile(
    r'name\s*=\s*["\']?([a-zA-Z_][\w:]*)',
    re.IGNORECASE,
)
_TOOLCALL_JSON_RE = re.compile(
    r"`(?:json)?\s*(\{.+?\})\s*$",
    re.DOTALL,
)
_TOOLCALL_ARTIFACT_RE = re.compile(
    r"</?(?:toolcall|argvalue|argkey|environment_details|thinking|thought)\b[^>]*>",
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
    "pa_system": "text",
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
    "speak": "pa_system",
    "announce": "pa_system",
    "pa": "pa_system",
    "popup": "panel_popup",
}

_LEADING_ACK_RE = re.compile(
    r"^(?:ok(?:ay)?|got it|on it|sure|understood|sounds good)[.!]?\s+",
    re.IGNORECASE,
)

_LOW_VALUE_PROGRESS_RE = re.compile(
    r"^(?:ok(?:ay)?|got it|on it|working on it|one sec|one second|sure|understood)[.!]?$",
    re.IGNORECASE,
)


def _is_low_value_progress_message(text: str) -> bool:
    return bool(_LOW_VALUE_PROGRESS_RE.match((text or "").strip()))


def _sanitize_response(text: str) -> str:
    if not text:
        return text

    # Remove full tool-call blocks first so argkey/argvalue payload text is not leaked.
    cleaned = re.sub(
        r"<toolcall\b[^>]*>.*?(?:</toolcall>|$)",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    cleaned = re.sub(
        r"<argkey\b[^>]*>\s*[^<]*\s*</argkey>\s*<argvalue\b[^>]*>.*?</argvalue>",
        "",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )
    cleaned = re.sub(
        r"<argvalue\b[^>]*>.*?</argvalue>",
        "",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )
    cleaned = _TOOLCALL_ARTIFACT_RE.sub("", cleaned)
    if cleaned != text:
        logger.warning(f"Stripped tool-call artifacts from response: {text[:200]}")

    cleaned = cleaned.strip()

    # Avoid repetitive standalone acknowledgement openers in final user-facing replies.
    stripped = _LEADING_ACK_RE.sub("", cleaned).strip()
    if stripped:
        cleaned = stripped

    cleaned = cleaned.strip()
    if _is_low_value_progress_message(cleaned):
        return ""
    if cleaned:
        return cleaned

    # If the content was purely tool markup, return empty instead of echoing raw tags.
    if (
        _TOOLCALL_ARTIFACT_RE.search(text)
        or "<toolcall" in text.lower()
        or "<argkey>" in text.lower()
        or "<argvalue>" in text.lower()
    ):
        return ""

    return text.strip()


def _parse_text_tool_calls(content: str) -> tuple[list[dict], str]:
    parsed = []
    remaining = _TOOLCALL_TAG_RE.sub("", content)
    for match in _TOOLCALL_TAG_RE.finditer(content):
        attr_blob = (match.group(1) or "").strip()
        body = (match.group(2) or "").strip()
        if not body:
            continue

        name_match = _TOOLCALL_NAME_RE.match(body)
        raw_name = ""
        rest = body
        if name_match:
            raw_name = name_match.group(1)
            rest = body[name_match.end():].strip()
        else:
            attr_name_match = _TOOLCALL_NAME_ATTR_RE.search(attr_blob)
            if not attr_name_match:
                continue
            raw_name = attr_name_match.group(1)

        tool_name = raw_name.split(":", 1)[-1] if ":" in raw_name else raw_name
        tool_name = _TEXT_TOOL_ALIASES.get(tool_name, tool_name)
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


async def _wait_for_agent(coro, timeout: int | None, activity: dict):
    """Await an agent run with an idle timeout instead of a wall clock.

    The deadline resets whenever the run makes progress — new tokens streaming
    in, a tool finishing, a new round starting — so an agent that is actively
    thinking or working is never cut off mid-stream. It only stops when
    nothing has happened for `timeout` seconds.
    """
    if timeout is None:
        return await coro
    task = asyncio.ensure_future(coro)
    try:
        while True:
            elapsed = time.monotonic() - float(activity.get("last", activity["start"]))
            remaining = timeout - elapsed
            if remaining <= 0:
                task.cancel()
                raise asyncio.TimeoutError()
            try:
                return await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
            except asyncio.TimeoutError:
                if task.done():
                    return task.result()
                continue
    except BaseException:
        if not task.done():
            task.cancel()
        raise


def _build_interruption_message(items: list[dict]) -> dict | None:
    """Turn drained interrupt-queue items into one user-role message.

    Handles the three sources that can arrive mid-run: new user text (sent
    while the agent was working), watchdog recovery interventions, and
    finished background-subagent results.
    """
    if not items:
        return None
    user_texts = [i.get("text", "") for i in items if i.get("source") not in {"stuck_watchdog", "subagent"}]
    system_texts = [i.get("text", "") for i in items if i.get("source") == "stuck_watchdog"]
    subagent_texts = [i.get("text", "") for i in items if i.get("source") == "subagent"]

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
    if subagent_texts:
        combined = "\n".join(f"- {t}" for t in subagent_texts)
        parts.append(
            "[SUBAGENT RESULT] A background subagent finished while you were working:\n\n"
            f"{combined}\n\n"
            "Incorporate this result into your work and your final answer."
        )

    if not parts:
        return None
    return {
        "role": "user",
        "content": "\n\n".join(parts),
    }


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
    speed_mode: str = "normal",
    reasoning_enabled: bool = True,
    activity_state: dict | None = None,
    raid_mode: bool = False,
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
    # Local models pay for every token of schema up front, so they get a core
    # set plus `load_tools` and pull the rest in when they need it.
    lazy_tools = using_default_tools and _lazy_tools_enabled(model)
    loaded_categories: set[str] = set()

    if lazy_tools:
        # Without this the model sees tools named in the system prompt that are
        # not in its schema, and stalls instead of asking for them.
        if messages and messages[0]["role"] == "system":
            messages[0] = {
                "role": "system",
                "content": messages[0]["content"] + "\n\n" + _LAZY_TOOLS_NOTE,
            }
        else:
            messages.insert(0, {"role": "system", "content": _LAZY_TOOLS_NOTE})

    def _build_tools() -> list[dict]:
        all_tools = _get_all_tools(
            include_subagent=allow_subagent, include_telegram=send_func is not None
        )
        return _visible_tools(all_tools, loaded_categories) if lazy_tools else all_tools

    if using_default_tools:
        tools = _build_tools()

    model_id = _MODELS.get(model.lower(), model)
    guard = _LoopGuard(is_orchestrator=_parent_agent_id is None)
    # Consecutive rounds whose only action was messaging the user.
    message_only_rounds = 0
    last_user_message = ""
    # Rounds that produced neither text nor a tool call.
    empty_rounds = 0

    # The orchestrator's agent id is the same string on every turn, so activity
    # events need a per-turn id as well -- without it the dashboard merges this
    # turn's streamed output into the previous turn's transcript (both are
    # "orchestrator, round 1").
    _run_id = uuid.uuid4().hex[:8]

    def _with_activity_meta(extra: dict | None = None) -> dict:
        payload = dict(extra or {})
        payload.setdefault("run", _run_id)
        if _parent_agent_id:
            payload.setdefault("parent_agent", _parent_agent_id)
        return payload

    def _touch_activity() -> None:
        if activity_state is not None:
            activity_state["last"] = time.monotonic()

    def _make_stream_sink(round_no: int):
        """Batch model deltas into activity events so the UI can render them live."""
        tracker = activity_tracker.get_tracker()
        buffers: dict[str, list[str]] = {}
        last_flush = {"at": time.monotonic()}
        seq = {"n": 0}

        async def _flush(kind: str, force: bool = False) -> None:
            chunk = "".join(buffers.get(kind, ()))
            if not chunk:
                return
            now = time.monotonic()
            if not force and len(chunk) < _STREAM_FLUSH_CHARS and (now - last_flush["at"]) < _STREAM_FLUSH_INTERVAL_S:
                return
            buffers[kind] = []
            last_flush["at"] = now
            seq["n"] += 1
            await tracker.emit(
                "stream", _agent_id, "Streaming", chunk[:_STREAM_MAX_EVENT_CHARS],
                _with_activity_meta({
                    "kind": kind,
                    "text": chunk[:_STREAM_MAX_EVENT_CHARS],
                    "seq": seq["n"],
                    "round": round_no,
                    "model": model_id,
                }),
            )

        async def _sink(kind: str, text: str) -> None:
            _touch_activity()
            buffers.setdefault(kind, []).append(text)
            await _flush(kind)

        async def _finish() -> None:
            for kind in list(buffers):
                await _flush(kind, force=True)

        _sink.finish = _finish
        return _sink

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

    _t = activity_tracker.get_tracker()

    # --- Long-run continuation -------------------------------------------------
    # The inner loop is one "budget" of rounds. A run that exhausts its budget
    # while still making progress earns a fresh one (up to
    # _MAX_TASK_CONTINUATIONS) instead of being cut off mid-task. Only stalled
    # or finished runs fall through to wrap-up.
    continuations_used = 0
    round_no = 0
    last_round_useful = False
    give_up = False

    while True:
        for _ in range(max_rounds):
            round_no += 1
            interrupts = _drain_interrupts()
            if interrupts:
                msg = _build_interruption_message(interrupts)
                if msg:
                    messages.append(msg)

            await _t.emit("thinking", _agent_id, "Thinking", f"Round {round_no} — calling model {model_id}", _with_activity_meta())
            _touch_activity()

            t0 = time.monotonic()
            sink = _make_stream_sink(round_no)
            try:
                response = await model_router.call_model(
                    model_id,
                    messages,
                    tools,
                    speed_mode=speed_mode,
                    reasoning_enabled=reasoning_enabled,
                    on_delta=sink,
                    user_id=user_id,
                )
            finally:
                await sink.finish()
            elapsed = (time.monotonic() - t0) * 1000

            reasoning_text = str(response.get("reasoning") or "")
            if reasoning_text.strip():
                await _t.emit(
                    "reasoning", _agent_id, "Reasoning", reasoning_text[:300],
                    _with_activity_meta({"text": reasoning_text[:_STREAM_MAX_EVENT_CHARS], "model": model_id}),
                )

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
                elif not str(content_text).strip() and empty_rounds < _MAX_EMPTY_ROUNDS:
                    # Reasoning models sometimes spend the whole turn inside their
                    # thinking block and stop without writing an answer. Ask once
                    # rather than handing the user an empty reply.
                    empty_rounds += 1
                    logger.warning(
                        f"{model_id} returned no content and no tool calls; nudging "
                        f"(attempt {empty_rounds}/{_MAX_EMPTY_ROUNDS})"
                    )
                    await _t.emit(
                        "thinking", _agent_id, "Empty response",
                        f"Model wrote nothing — retrying ({empty_rounds}/{_MAX_EMPTY_ROUNDS})",
                        _with_activity_meta({"duration_ms": round(elapsed), "model": model_id}),
                    )
                    messages.append({
                        "role": "user",
                        "content": (
                            "[SYSTEM] Your last turn produced no output at all. Write your answer "
                            "to the user now as plain text, or call a tool if you still need "
                            "information first. Do not end your turn empty again."
                        ),
                    })
                    continue
                else:
                    await _t.emit(
                        "done", _agent_id, "Responded", f"Final answer in {elapsed:.0f}ms",
                        _with_activity_meta({"duration_ms": round(elapsed), "text": str(content_text)[:_STREAM_MAX_EVENT_CHARS]}),
                    )
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

            # Refuse repeats before executing anything: a model stuck in a loop gets
            # told to change course instead of re-running the same side effect.
            results: dict[str, str] = {}
            allowed_calls = []
            for tc in tool_calls:
                blocked = guard.check(tc["name"], tc["arguments"] if isinstance(tc["arguments"], dict) else {})
                if blocked is None:
                    allowed_calls.append(tc)
                    continue
                results[tc["id"]] = blocked
                logger.warning(f"Loop guard blocked repeated call to {tc['name']} by {_agent_id}")
                await _t.emit(
                    "blocked", _agent_id, "Repeat blocked", f"Skipped duplicate {tc['name']} call",
                    _with_activity_meta({"tool": tc["name"], "detail": blocked}),
                )

            subagent_calls = [tc for tc in allowed_calls if tc["name"] == "spawn_subagent"]
            other_calls = [tc for tc in allowed_calls if tc["name"] != "spawn_subagent"]

            tools_dirty = False

            def _load_categories(names) -> list[str]:
                """Mark categories loaded; returns the ones that were new."""
                nonlocal tools_dirty
                added = []
                for raw in names or []:
                    category = str(raw).strip().lower()
                    if category in _TOOL_CATEGORIES and category not in loaded_categories:
                        loaded_categories.add(category)
                        added.append(category)
                if added:
                    tools_dirty = True
                return added

            for tc in other_calls:
                if lazy_tools and tc["name"] == _LOAD_TOOLS_NAME:
                    args = tc["arguments"] if isinstance(tc["arguments"], dict) else {}
                    requested = args.get("categories") or []
                    if isinstance(requested, str):
                        requested = [requested]
                    added = _load_categories(requested)
                    unknown = [
                        str(c) for c in requested
                        if str(c).strip().lower() not in _TOOL_CATEGORIES
                    ]
                    available = sorted(
                        name for category in loaded_categories
                        for name in _TOOL_CATEGORIES[category][1]
                    )
                    results[tc["id"]] = json.dumps({
                        "loaded": added,
                        "already_loaded": sorted(loaded_categories - set(added)),
                        "unknown_categories": unknown,
                        "tools_now_available": available,
                        "instruction": "These tools are ready to use. Call the one you need now.",
                    })
                    await _t.emit(
                        "tool", _agent_id, "Tools loaded", ", ".join(added) or "nothing new",
                        _with_activity_meta({"tool": _LOAD_TOOLS_NAME, "categories": added}),
                    )
                    continue

                # A model may call a deferred tool by name without loading it first.
                # Load the category and run it rather than burning a round on an error.
                if lazy_tools:
                    dynamic_names = {
                        d.get("function", {}).get("name", "")
                        for d in dynamic_tools.get_tool_definitions()
                    }
                    category = _category_of_tool(tc["name"], dynamic_names)
                    if category and category not in loaded_categories:
                        _load_categories([category])

                results[tc["id"]] = await _execute_tool_call(
                    tc["name"], tc["arguments"],
                    user_id if user_id is not None else 0,
                    send_func=send_func,
                    allow_subagent=allow_subagent,
                    _agent_id=_agent_id,
                    _parent_agent_id=_parent_agent_id,
                    _interrupt_queue=interrupt_queue,
                )
                _touch_activity()

            if subagent_calls:
                async def _do_subagent(tc):
                    r = await _execute_tool_call(
                        tc["name"], tc["arguments"],
                        user_id if user_id is not None else 0,
                        send_func=send_func,
                        allow_subagent=allow_subagent,
                        _agent_id=_agent_id,
                        _parent_agent_id=_parent_agent_id,
                        _interrupt_queue=interrupt_queue,
                    )
                    _touch_activity()
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
        
            round_had_useful_result = False
            for tc in tool_calls:
                result_text = results.get(tc["id"], json.dumps({"error": "execution failed"}))
                if not _tool_result_is_failure(result_text):
                    round_had_useful_result = True
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": _truncate_tool_result(result_text),
                })
                await _t.emit(
                    "result", _agent_id, "Tool result", f"{tc['name']} → {str(result_text)[:160]}",
                    _with_activity_meta({"tool": tc["name"], "text": str(result_text)[:_STREAM_MAX_EVENT_CHARS]}),
                )

            if using_default_tools and (
                tools_dirty
                or any(tc["name"] in {"create_tool", "delete_tool"} for tc in tool_calls)
            ):
                tools = _build_tools()

            # A round spent only messaging the user IS the model's answer — weaker
            # models reply through send_telegram_message instead of returning text,
            # then read the "{sent: true}" result as a fresh user turn and invent
            # more work. Close the turn out instead of handing them another round.
            messaged = [tc for tc in allowed_calls if tc["name"] in _USER_FACING_MESSAGE_TOOLS]
            if messaged and len(messaged) == len(allowed_calls):
                message_only_rounds += 1
                texts = [
                    str(tc["arguments"].get("message") or tc["arguments"].get("text") or "").strip()
                    for tc in messaged if isinstance(tc["arguments"], dict)
                ]
                spoken = "\n\n".join(t for t in texts if t)
                if spoken:
                    last_user_message = spoken
                # One round of grace for "on it, starting now"; otherwise the model
                # has said its piece and the turn is over.
                if spoken and not _message_defers_work(spoken):
                    await _t.emit(
                        "done", _agent_id, "Responded",
                        "Answered the user through a message tool",
                        _with_activity_meta({"text": spoken[:_STREAM_MAX_EVENT_CHARS], "via": "send_telegram_message"}),
                    )
                    return AlreadySentReply(spoken)
                if message_only_rounds >= 2:
                    await _t.emit(
                        "done", _agent_id, "Responded",
                        "Only sent messages for two rounds — closing the turn",
                        _with_activity_meta({"text": last_user_message[:_STREAM_MAX_EVENT_CHARS]}),
                    )
                    return AlreadySentReply(last_user_message)
                messages.append({
                    "role": "user",
                    "content": (
                        "[SYSTEM] Your message was delivered to the user. Do not send it again. "
                        "The tool results above are tool output, not new messages from the user. "
                        "If you have finished, reply with your final answer as plain text and call no tools. "
                        "Otherwise continue with real work."
                    ),
                })
            else:
                message_only_rounds = 0

            # A round counts as stalled when the guard blocked everything OR when
            # every call that did run came back an error. The second half is what
            # catches a jammed backend (rate limiter, broken CLI, dead credential):
            # previously those rounds read as progress and reset the counter, so the
            # loop ran to its round cap retrying a call that could not succeed.
            made_no_progress = bool(tool_calls) and (not allowed_calls or not round_had_useful_result)
            last_round_useful = not made_no_progress
            if guard.note_round(made_no_progress):
                logger.warning(f"{_agent_id} made no progress for {guard.stalled_rounds} rounds; ending tool loop")
                await _t.emit(
                    "blocked", _agent_id, "Loop stopped",
                    "No tool call produced a usable result — stopping and summarizing instead.",
                    _with_activity_meta(),
                )
                messages.append({
                    "role": "user",
                    "content": (
                        "[SYSTEM] Your tool calls stopped producing usable results — they were either exact "
                        "repeats or every one returned an error. Tool use is now disabled for this turn. "
                        "Do not retry. Write your final answer as plain text, and if something is broken, "
                        "tell the user plainly what failed and quote the error you received."
                    ),
                })
                give_up = True
                break
    

        # Budget exhausted. If the agent was still making progress, grant a
        # fresh round budget (up to _MAX_TASK_CONTINUATIONS) instead of dying
        # with "break the task into smaller pieces". Compaction keeps the
        # context bounded across long runs.
        if give_up:
            break
        if last_round_useful and continuations_used < max(0, _MAX_TASK_CONTINUATIONS):
            continuations_used += 1
            # A longer task legitimately needs more user-facing updates.
            guard.grant_messages(2)
            compacted = await _compact_run_context(messages, task_text=prompt)
            if compacted:
                await _t.emit(
                    "thinking", _agent_id, "Context compacted",
                    "Older tool results were stubbed out to keep the run within budget",
                    _with_activity_meta({"continuation": continuations_used, "round": round_no}),
                )
            messages.append({
                "role": "user",
                "content": (
                    f"[SYSTEM CONTINUATION] You just used the last round of your current tool "
                    f"budget, and the task is still in progress. A fresh budget of {max_rounds} "
                    f"rounds has been granted (continuation {continuations_used} of "
                    f"{_MAX_TASK_CONTINUATIONS}). Continue exactly where you left off. Do NOT "
                    "restart, re-plan from scratch, or redo finished work. Keep making progress "
                    "toward finishing the user's original request, and write your final answer "
                    "as soon as the work is genuinely done."
                ),
            })
            await _t.emit(
                "thinking", _agent_id, "Continuing task",
                f"Round budget used while still productive — continuing (segment {continuations_used + 1})",
                _with_activity_meta({"continuation": continuations_used, "round": round_no, "context_compacted": compacted}),
            )
            continue
        break

    async def _wrap_up() -> dict:
        """Final, tool-free call so the agent always produces a written answer."""
        sink = _make_stream_sink(max_rounds + 1)
        try:
            return await model_router.call_model(
                model_id,
                messages,
                None,
                speed_mode=speed_mode,
                reasoning_enabled=reasoning_enabled,
                on_delta=sink,
                user_id=user_id,
            )
        finally:
            await sink.finish()

    try:
        interrupt_texts = _drain_interrupts()
        if interrupt_texts:
            msg = _build_interruption_message(interrupt_texts)
            if msg:
                messages.append(msg)
        response = await _wrap_up()
        final_content = response.get("content", "I could not generate a response right now.")

        interrupt_texts = _drain_interrupts()
        if interrupt_texts:
            msg = _build_interruption_message(interrupt_texts)
            if msg:
                messages.append(response.get("message", {"role": "assistant", "content": final_content}))
                messages.append(msg)
                response = await _wrap_up()
                final_content = response.get("content", final_content)

        sanitized = _sanitize_response(final_content)
        await _t.emit(
            "done", _agent_id, "Responded", "Final answer",
            _with_activity_meta({"text": str(sanitized or final_content)[:_STREAM_MAX_EVENT_CHARS]}),
        )
        if sanitized and sanitized.strip():
            return sanitized
        return (
            "I stopped making usable progress and ran out of my processing budget for this task "
            "(including continuations). Try asking me to continue with a specific next step, "
            "or split the task into smaller pieces."
        )
    except Exception:
        logger.exception(f"{_agent_id} wrap-up call failed")
        return (
            "I stopped making usable progress and ran out of my processing budget for this task "
            "(including continuations). Try asking me to continue with a specific next step, "
            "or split the task into smaller pieces."
        )


# ---------------------------------------------------------------------------
# Subagent runner
# ---------------------------------------------------------------------------

async def _run_subagent(
    user_id: int,
    role: str,
    task: str,
    context: str = "",
    send_func: SendFunc | None = None,
    activity_state: dict | None = None,
    _agent_id: str = "subagent",
    _parent_agent_id: str | None = None,
) -> str:
    model = db.get_model(user_id)
    speed_mode = db.get_speed_mode(user_id)
    reasoning_enabled = db.get_reasoning_enabled(user_id)
    raid_mode = db.get_raid_mode(user_id)
    subagent_rounds, subagent_calls = _subagent_limits_for_speed(speed_mode, raid_mode)
    system = _build_subagent_system(user_id, role, task, context, raid_mode=raid_mode)
    history = db.get_history(user_id)
    return await _run_agent(
        model, task,
        system=system,
        history=history,
        user_id=user_id,
        send_func=send_func,
        allow_subagent=False,
        max_rounds=subagent_rounds,
        max_calls_per_round=subagent_calls,
        speed_mode=speed_mode,
        reasoning_enabled=reasoning_enabled,
        activity_state=activity_state,
        raid_mode=raid_mode,
        _agent_id=_agent_id,
        _parent_agent_id=_parent_agent_id,
    )


# ---------------------------------------------------------------------------
# Summarize (async)
# ---------------------------------------------------------------------------

async def _maybe_summarize(
    user_id: int,
    model: str,
    speed_mode: str = "normal",
    reasoning_enabled: bool = True,
) -> None:
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
        summary = await model_router.call_model_simple(
            model_id,
            system_msg,
            f"Summarize this conversation:\n\n{conversation}",
            speed_mode=speed_mode,
            reasoning_enabled=reasoning_enabled,
            user_id=user_id,
        )
        db.set_summary(user_id, summary)
        db.compact_history(user_id, db.HISTORY_WINDOW)
    except Exception:
        logger.exception("Summarization failed")


def _schedule_summary(
    user_id: int,
    model: str,
    speed_mode: str,
    reasoning_enabled: bool,
) -> None:
    async def _task() -> None:
        await _maybe_summarize(
            user_id,
            model,
            speed_mode=speed_mode,
            reasoning_enabled=reasoning_enabled,
        )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    loop.create_task(_task())


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
    speed_mode = db.get_speed_mode(user_id)
    reasoning_enabled = db.get_reasoning_enabled(user_id)
    raid_mode = db.get_raid_mode(user_id)
    base_model = model_override or db.get_model(user_id)
    model = base_model if model_override else _pick_runtime_model(base_model, speed_mode)
    if not model_override and speed_mode == "normal":
        triaged_model = await _triage_model(base_model, text)
        if triaged_model:
            model = triaged_model
    agent_rounds, agent_calls, orchestrator_timeout = _agent_limits_for_speed(speed_mode, raid_mode)
    run_activity = {"start": time.monotonic(), "last": time.monotonic()}
    history = db.get_history(user_id)
    system = _build_system(user_id, text, relevant_memories=await _rerank_memories(user_id, text), raid_mode=raid_mode)
    
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
            response = await model_router.call_model(
                vision_model_id,
                messages,
                None,
                speed_mode=speed_mode,
                reasoning_enabled=reasoning_enabled,
                user_id=user_id,
            )
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
        
        try:
            reply = await _wait_for_agent(
                _run_agent(
                    model, agent_prompt,
                    system=system,
                    history=history,
                    user_id=user_id,
                    send_func=send_func,
                    allow_subagent=True,
                    max_rounds=agent_rounds,
                    max_calls_per_round=agent_calls,
                    interrupt_event=interrupt_event,
                    interrupt_queue=interrupt_queue,
                    speed_mode=speed_mode,
                    reasoning_enabled=reasoning_enabled,
                    activity_state=run_activity,
                    raid_mode=raid_mode,
                ),
                orchestrator_timeout,
                run_activity,
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
        try:
            reply = await _wait_for_agent(
                _run_agent(
                    model, text,
                    system=system,
                    history=history,
                    user_id=user_id,
                    send_func=send_func,
                    allow_subagent=True,
                    max_rounds=agent_rounds,
                    max_calls_per_round=agent_calls,
                    interrupt_event=interrupt_event,
                    interrupt_queue=interrupt_queue,
                    speed_mode=speed_mode,
                    reasoning_enabled=reasoning_enabled,
                    activity_state=run_activity,
                    raid_mode=raid_mode,
                ),
                orchestrator_timeout,
                run_activity,
            )
        except asyncio.TimeoutError:
            reply = (
                f"I hit the processing timeout ({orchestrator_timeout}s) and stopped to avoid hanging. "
                "Please retry with a narrower request."
            )

    db.add_message(user_id, "user", text)
    # Messages the user sent mid-run (delivered as interruptions) belong in
    # history between the original message and this reply.
    try:
        for injected_text in interrupt_hub.take_injected(user_id, interrupt_queue):
            db.add_message(user_id, "user", injected_text)
    except Exception:
        logger.exception("Could not persist injected messages")
    db.add_message(user_id, "assistant", reply)
    _schedule_summary(user_id, model, speed_mode=speed_mode, reasoning_enabled=reasoning_enabled)
    sanitized = _sanitize_response(reply)
    # Preserve the "already delivered" marker through sanitising.
    if was_already_sent(reply):
        return AlreadySentReply(sanitized)
    return sanitized


async def respond_with_image(user_id: int, text: str, image_b64: str, send_func: SendFunc | None = None) -> str:
    speed_mode = db.get_speed_mode(user_id)
    reasoning_enabled = db.get_reasoning_enabled(user_id)
    agent_rounds, agent_calls, orchestrator_timeout = _agent_limits_for_speed(speed_mode)
    run_activity = {"start": time.monotonic(), "last": time.monotonic()}

    vision_model = db.get_image_model(user_id)
    base_main_model = db.get_model(user_id)
    main_model = _pick_runtime_model(base_main_model, speed_mode)
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
        response = await model_router.call_model(
            vision_model_id,
            messages,
            None,
            speed_mode=speed_mode,
            reasoning_enabled=reasoning_enabled,
            user_id=user_id,
        )
        image_analysis = response.get("content") or "I could not analyze the image."
    except Exception as e:
        logger.exception(f"Image analysis failed for user {user_id}")
        image_analysis = f"Error analyzing image: {e}"

    try:
        agent_prompt = f"""The user sent an image with the message: "{text}"

Here is my analysis of the image:
{image_analysis}

Based on this analysis and the user's request, proceed with any tasks needed. If the user is asking for something to be created or done, use the available tools to accomplish it."""
        
        reply = await _wait_for_agent(
            _run_agent(
                main_model, agent_prompt,
                system=system,
                history=history,
                user_id=user_id,
                send_func=send_func,
                allow_subagent=True,
                max_rounds=agent_rounds,
                max_calls_per_round=agent_calls,
                speed_mode=speed_mode,
                reasoning_enabled=reasoning_enabled,
                activity_state=run_activity,
            ),
            orchestrator_timeout,
            run_activity,
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
    _schedule_summary(user_id, main_model, speed_mode=speed_mode, reasoning_enabled=reasoning_enabled)
    return _sanitize_response(reply)
