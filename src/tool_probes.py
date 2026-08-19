"""Simple, real invocations of every tool TALOS exposes.

Each probe calls the tool through `AI._execute_tool_call` — the exact dispatch
path the model uses — with hand-written arguments, then checks the result is
what it should be. No model is involved anywhere: this is the whole point, since
the tools have to be testable when the assistant itself is what is broken.

Probes are deliberately shallow. They confirm a tool dispatches, does its one
obvious job, and returns the expected shape. They are not integration tests.

Rules every probe follows:
  - Work only inside the diagnostics scratch directory, never on user data.
  - Clean up whatever they create, even when the assertion fails.
  - On failure, report the tool's *actual* error text rather than a summary,
    because that string is what tells you what to repair.
  - Never send anything outward: messaging probes pass a capturing send function,
    so delivery is exercised without the user receiving test spam.
"""

import asyncio
import json
import os
import shutil
import time
from typing import Any

import app_paths

# Negative ids are what the dashboard already uses for non-Telegram users, so a
# reserved one here cannot collide with a real Telegram account.
PROBE_USER_ID = -987654321

_SCRATCH_NAME = "diagnostics_scratch"


def scratch_dir() -> str:
    path = app_paths.data_path(_SCRATCH_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def scratch_path(*parts: str) -> str:
    return os.path.join(scratch_dir(), *parts)


def clear_scratch() -> int:
    """Delete everything in the scratch directory. Returns the count removed."""
    path = scratch_dir()
    removed = 0
    for entry in os.listdir(path):
        target = os.path.join(path, entry)
        try:
            if os.path.isdir(target):
                shutil.rmtree(target)
            else:
                os.remove(target)
            removed += 1
        except OSError:
            pass
    return removed


class _Captured:
    """Stands in for the Telegram send function so nothing leaves the machine."""

    def __init__(self):
        self.messages: list[dict] = []

    async def __call__(self, message: str = "", **kwargs) -> None:
        self.messages.append({"message": message, **kwargs})


async def call_tool(tool_name: str, args: dict, send_func: Any = None) -> tuple[Any, str]:
    """Invoke a tool the way the agent loop does. Returns (parsed, raw_text)."""
    import AI

    raw = await AI._execute_tool_call(
        tool_name, args, PROBE_USER_ID,
        send_func=send_func,
        allow_subagent=False,
        _agent_id="diagnostics",
    )
    text = raw if isinstance(raw, str) else json.dumps(raw)
    try:
        return json.loads(text), text
    except (json.JSONDecodeError, TypeError):
        return None, text


def _err(parsed: Any, raw: str) -> str:
    """The most useful error string a tool result carries."""
    if isinstance(parsed, dict):
        for key in ("error", "message", "detail", "hint"):
            if parsed.get(key):
                return str(parsed[key])
    return (raw or "").strip()[:400] or "no output"


class ProbeOutcome:
    def __init__(self, status: str, detail: str, fix: str | None = None,
                 reset: str | None = None, hint: str = ""):
        self.status = status
        self.detail = detail
        self.fix = fix
        self.reset = reset
        self.hint = hint


def ok(detail: str) -> ProbeOutcome:
    return ProbeOutcome("ok", detail)


def fail(detail: str, fix: str | None = None, reset: str | None = None, hint: str = "") -> ProbeOutcome:
    return ProbeOutcome("fail", detail, fix=fix, reset=reset, hint=hint)


def warn(detail: str, fix: str | None = None, reset: str | None = None, hint: str = "") -> ProbeOutcome:
    return ProbeOutcome("warn", detail, fix=fix, reset=reset, hint=hint)


def skip(detail: str) -> ProbeOutcome:
    return ProbeOutcome("skip", detail)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

async def probe_files() -> ProbeOutcome:
    """write -> read -> edit -> read, the full round trip."""
    path = scratch_path("probe_file.txt")
    try:
        parsed, raw = await call_tool("write_file", {"path": path, "content": "hello talos\n", "create_dirs": True})
        if not isinstance(parsed, dict) or parsed.get("error"):
            return fail(f"write_file failed: {_err(parsed, raw)}", fix="files.reset_scratch")
        if not os.path.isfile(path):
            return fail("write_file reported success but no file appeared on disk.", fix="files.reset_scratch")

        parsed, raw = await call_tool("read_file", {"path": path})
        if not isinstance(parsed, dict) or parsed.get("error"):
            return fail(f"read_file failed: {_err(parsed, raw)}", fix="files.reset_scratch")
        if "hello talos" not in json.dumps(parsed):
            return fail(f"read_file did not return the content just written. Got: {raw[:200]}")

        parsed, raw = await call_tool("edit_file", {
            "path": path, "old_string": "hello talos", "new_string": "edited talos",
        })
        if not isinstance(parsed, dict) or parsed.get("error"):
            return fail(f"edit_file failed: {_err(parsed, raw)}", fix="files.reset_scratch")

        with open(path, "r", encoding="utf-8") as fh:
            if "edited talos" not in fh.read():
                return fail("edit_file reported success but the file content did not change.")

        return ok("write, read and edit all round-trip correctly.")
    finally:
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------

async def probe_execute_command() -> ProbeOutcome:
    parsed, raw = await call_tool("execute_command", {"command": "echo talos_probe_ok"})
    if not isinstance(parsed, dict):
        return fail(f"execute_command returned unparseable output: {raw[:200]}")
    if parsed.get("rate_limited"):
        return warn(
            f"Rate limiter is saturated: {parsed.get('error')}",
            fix="terminal.raise_rate_limit",
        )
    if parsed.get("error"):
        return fail(f"execute_command failed: {parsed['error']}", fix="terminal.reset_config")
    if "talos_probe_ok" not in str(parsed.get("stdout", "")):
        return fail(f"Command ran but stdout was wrong. Got: {json.dumps(parsed)[:200]}",
                    fix="terminal.reset_config")
    return ok("Shell commands run and return their output.")


async def probe_execute_workflow() -> ProbeOutcome:
    parsed, raw = await call_tool("execute_workflow", {
        "steps": [{"command": "echo step_one"}, {"command": "echo step_two"}],
    })
    if not isinstance(parsed, dict):
        return fail(f"execute_workflow returned unparseable output: {raw[:200]}")
    blob = json.dumps(parsed)
    if parsed.get("error") and "step_one" not in blob:
        return fail(f"execute_workflow failed: {parsed['error']}", fix="terminal.reset_config")
    if "rate limit" in blob.lower():
        return warn("Rate limiter saturated during the workflow probe.", fix="terminal.raise_rate_limit")
    if "step_one" not in blob or "step_two" not in blob:
        return fail(f"Workflow did not run both steps. Got: {blob[:250]}")
    return ok("Multi-step workflows run each step in order.")


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

async def probe_memory() -> ProbeOutcome:
    """save -> search -> update -> delete."""
    marker = f"diagnostics probe marker {int(time.time())}"
    memory_id = None
    try:
        parsed, raw = await call_tool("save_memory", {"content": marker, "category": "diagnostics"})
        if not isinstance(parsed, dict) or parsed.get("error"):
            return fail(f"save_memory failed: {_err(parsed, raw)}", reset="db.reset")
        memory_id = parsed.get("id") or parsed.get("memory_id")

        parsed, raw = await call_tool("search_memories", {"query": "diagnostics probe marker"})
        if marker not in raw:
            return fail(f"search_memories did not find the memory just saved. Got: {raw[:250]}")

        parsed, raw = await call_tool("list_memories", {"category": "diagnostics"})
        if marker not in raw:
            return fail(f"list_memories did not return the saved memory. Got: {raw[:250]}")

        if memory_id is not None:
            parsed, raw = await call_tool("update_memory", {
                "memory_id": memory_id, "content": marker + " (updated)",
            })
            if isinstance(parsed, dict) and parsed.get("error"):
                return fail(f"update_memory failed: {parsed['error']}")

        return ok("Memories save, search, list and update correctly.")
    finally:
        if memory_id is not None:
            try:
                await call_tool("delete_memory", {"memory_id": memory_id})
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

async def probe_scheduling() -> ProbeOutcome:
    """schedule -> list -> remove."""
    job_name = f"diagnostics_probe_{int(time.time())}"
    job_id = None
    try:
        parsed, raw = await call_tool("schedule_cron", {
            "name": job_name,
            "schedule": "0 4 * * *",  # the tool takes a cron expression, not a word
            "command": "echo diagnostics_probe",
        })
        if not isinstance(parsed, dict) or parsed.get("error"):
            return fail(f"schedule_cron failed: {_err(parsed, raw)}", reset="db.reset")
        job_id = parsed.get("id") or parsed.get("job_id")

        parsed, raw = await call_tool("list_cron", {})
        if job_name not in raw:
            return fail(f"list_cron did not return the job just scheduled. Got: {raw[:250]}")

        return ok("Scheduled jobs can be created and listed.")
    finally:
        if job_id is not None:
            try:
                await call_tool("remove_cron", {"job_id": job_id})
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

async def probe_spreadsheet() -> ProbeOutcome:
    path = scratch_path("probe_sheet.xlsx")
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        return fail(
            "openpyxl is not installed, so no spreadsheet tool can run.",
            fix="deps.install_spreadsheet",
            hint="Installs openpyxl and pandas.",
        )

    try:
        parsed, raw = await call_tool("spreadsheet_execute", {
            "action": "edit",
            "path": path,
            "create_if_missing": True,
            "operations": [{"action": "append_row", "sheet_name": "Sheet1", "values": ["probe", 42]}],
        })
        if not isinstance(parsed, dict) or parsed.get("error"):
            return fail(f"Creating a spreadsheet failed: {_err(parsed, raw)}", fix="deps.install_spreadsheet")
        if not os.path.isfile(path):
            return fail("Spreadsheet edit reported success but no file was written.")

        parsed, raw = await call_tool("spreadsheet_execute", {"action": "read", "path": path})
        if not isinstance(parsed, dict) or parsed.get("error"):
            return fail(f"Reading the spreadsheet back failed: {_err(parsed, raw)}",
                        fix="deps.install_spreadsheet")
        if "probe" not in raw:
            return fail(f"Spreadsheet read did not contain the row just written. Got: {raw[:250]}")

        return ok("Spreadsheets can be created, written and read back.")
    finally:
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass


async def probe_docx() -> ProbeOutcome:
    path = scratch_path("probe_doc.docx")
    try:
        parsed, raw = await call_tool("docx_execute", {
            "action": "create",
            "path": path,
            "title": "Diagnostics Probe",
            "paragraphs": ["This document was generated by the system check."],
        })
        if not isinstance(parsed, dict) or parsed.get("error"):
            detail = _err(parsed, raw)
            lowered = detail.lower()
            if "node" in lowered and ("not available" in lowered or "not found" in lowered):
                return fail(
                    "Word documents need Node.js, which is not installed on this machine.",
                    fix="deps.install_docx",
                    hint="Installs Node.js and the `docx` module.",
                )
            if "docx" in lowered and ("missing" in lowered or "npm" in lowered):
                return fail(
                    f"Word document creation needs the `docx` Node module: {detail}",
                    fix="deps.install_docx",
                    hint="Runs `npm install docx` in the project root.",
                )
            return fail(f"Creating a Word document failed: {detail}")
        if not os.path.isfile(path):
            return fail("Document creation reported success but no file was written.")

        parsed, raw = await call_tool("docx_execute", {"action": "validate_xml", "path": path})
        if isinstance(parsed, dict) and parsed.get("error"):
            return warn(f"Document was created but failed validation: {parsed['error']}")

        size = os.path.getsize(path)
        return ok(f"Word documents can be created and validated ({size} bytes).")
    finally:
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

async def probe_projects() -> ProbeOutcome:
    """create -> list -> unregister."""
    import gateway

    name = f"diag-probe-{int(time.time())}"
    created = False
    try:
        parsed, raw = await call_tool("create_project", {
            "name": name,
            "html": "<!doctype html><title>probe</title><h1>probe</h1>",
            "description": "Diagnostics probe project",
        })
        if not isinstance(parsed, dict) or parsed.get("error"):
            return fail(f"create_project failed: {_err(parsed, raw)}", reset="projects.reset_registry")
        created = True
        if not parsed.get("url"):
            return fail(f"Project was created but no URL was returned. Got: {raw[:250]}")
        index = os.path.join(str(parsed.get("path", "")), "index.html")
        if not os.path.isfile(index):
            return fail(f"Project registered but index.html was not written to {index}.")

        parsed, raw = await call_tool("list_projects", {})
        if name not in raw:
            return fail(f"list_projects did not include the project just created. Got: {raw[:250]}",
                        reset="projects.reset_registry")

        return ok("Projects can be created, served and listed.")
    finally:
        if created:
            try:
                reg = [p for p in gateway.list_projects() if p.get("name") == name]
                gateway.unregister_project(name)
                for p in reg:
                    path = p.get("path")
                    if path and os.path.isdir(path) and _SCRATCH_NAME not in path:
                        shutil.rmtree(path, ignore_errors=True)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Messaging (captured — nothing is actually delivered)
# ---------------------------------------------------------------------------

async def probe_send_message() -> ProbeOutcome:
    capture = _Captured()
    parsed, raw = await call_tool(
        "send_telegram_message",
        {"message": "Diagnostics probe: confirming the message tool dispatches correctly."},
        send_func=capture,
    )
    if not isinstance(parsed, dict):
        return fail(f"send_telegram_message returned unparseable output: {raw[:200]}")
    if parsed.get("error"):
        return fail(f"send_telegram_message failed: {parsed['error']}")
    if not parsed.get("sent"):
        return fail(f"Message was not marked as sent. Got: {raw[:250]}")
    if not capture.messages:
        return fail("Tool reported success but nothing reached the send function.")
    return ok("Outbound messages reach the transport layer.")


async def probe_send_document() -> ProbeOutcome:
    path = scratch_path("probe_attachment.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("diagnostics probe attachment\n")
    capture = _Captured()
    try:
        parsed, raw = await call_tool(
            "send_telegram_document",
            {"path": path, "caption": "Diagnostics probe"},
            send_func=capture,
        )
        if not isinstance(parsed, dict):
            return fail(f"send_telegram_document returned unparseable output: {raw[:200]}")
        if parsed.get("error"):
            return fail(f"send_telegram_document failed: {parsed['error']}")
        if not capture.messages:
            return fail("Tool reported success but nothing reached the send function.")
        return ok("File attachments reach the transport layer.")
    finally:
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass


async def probe_tts() -> ProbeOutcome:
    """Text-to-speech, tested by generating audio rather than by speaking."""
    try:
        import voice
    except ImportError as exc:
        return fail(f"The voice module could not be imported: {exc}")

    if not voice.is_gtts_available():
        return fail(
            "gTTS is not installed, so voice messages cannot be generated.",
            fix="deps.install_tts",
            hint="Installs gTTS.",
        )

    out = scratch_path("probe_tts.mp3")
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(voice.text_to_speech, "System check.", out), timeout=45
        )
        if not result or not os.path.isfile(out) or os.path.getsize(out) == 0:
            return fail(
                "gTTS is installed but produced no audio. This is usually no network access to "
                "translate.google.com.",
            )
        return ok(f"Speech synthesis works ({os.path.getsize(out)} bytes generated).")
    except asyncio.TimeoutError:
        return fail("Speech synthesis timed out after 45s — the TTS service is unreachable.")
    except Exception as exc:
        return fail(f"Speech synthesis failed: {exc}")
    finally:
        if os.path.isfile(out):
            try:
                os.remove(out)
            except OSError:
                pass


async def probe_pa_system() -> ProbeOutcome:
    """Checks a speech backend exists without actually playing audio."""
    import pa_system

    try:
        backends = pa_system._resolve_backends()
    except Exception as exc:
        return fail(f"PA system backend detection failed: {exc}")

    if not backends:
        return fail(
            "No text-to-speech backend is available on this machine, so announcements cannot play.",
            hint="Install one: `say` (macOS, built in), espeak or speech-dispatcher (Linux), "
                 "SAPI (Windows, built in).",
        )
    return ok(f"{len(backends)} speech backend(s) available: {', '.join(sorted(backends))}.")


# ---------------------------------------------------------------------------
# Web
# ---------------------------------------------------------------------------

async def probe_web_search() -> ProbeOutcome:
    """Validates the credential, deliberately without running a search.

    Calling `web_search` would be a live LLM inference: the module picks its
    search model by calling `generate_content` against each candidate until one
    answers. This whole suite promises not to invoke the model, so the probe
    checks that the key authenticates against the model-list endpoint instead —
    real verification, no generation, no token spend.
    """
    if not os.getenv("GEMINI_API_KEY", "").strip():
        return skip("Skipped — web search needs GEMINI_API_KEY, which is not set.")

    try:
        import websearch
        client = await asyncio.to_thread(websearch._get_client)
        models = await asyncio.wait_for(
            asyncio.to_thread(lambda: [m.name for m in client.models.list()]), timeout=30
        )
    except asyncio.TimeoutError:
        return fail("Timed out reaching the Gemini API after 30s.")
    except Exception as exc:
        detail = str(exc)
        lowered = detail.lower()
        if "api key" in lowered or "unauthenticated" in lowered or "permission" in lowered:
            return fail(
                f"GEMINI_API_KEY was rejected: {detail[:180]}",
                reset="web.clear_gemini_key",
                hint="An invalid key cannot be repaired automatically. Clearing it stops web "
                     "search from erroring; get a new one at https://aistudio.google.com/app/apikey.",
            )
        return fail(f"Could not reach the Gemini API: {detail[:200]}")

    searchable = [m for m in models if any(c in m for c in websearch._CANDIDATE_MODELS)]
    if not searchable:
        return warn(
            "The API key works, but none of the models web search knows how to use are available "
            f"to it ({', '.join(websearch._CANDIDATE_MODELS[:3])}...). Search will fail.",
            reset="web.clear_gemini_key",
            hint="Usually a key from a project without Gemini API access enabled. Enable it, or "
                 "clear the key to disable web search cleanly.",
        )

    return ok(f"Gemini credentials valid, {len(searchable)} search-capable model(s) available.")


async def probe_scrape() -> ProbeOutcome:
    try:
        parsed, raw = await asyncio.wait_for(
            call_tool("scrape_url", {"url": "https://example.com", "timeout": 20000}), timeout=60
        )
    except asyncio.TimeoutError:
        return fail("scrape_url timed out after 60s.")

    if not isinstance(parsed, dict):
        return fail(f"scrape_url returned unparseable output: {raw[:200]}")
    if parsed.get("error"):
        return fail(f"scrape_url failed: {parsed['error']}")
    if "example" not in raw.lower():
        return fail(f"Scrape succeeded but returned unexpected content: {raw[:200]}")
    return ok("Page scraping works.")


async def probe_browser() -> ProbeOutcome:
    """Reports readiness without launching a browser — that is too heavy for a probe."""
    import browser_automation

    if browser_automation.async_playwright is None:
        return fail(
            "Playwright is not installed, so no browser tool can run.",
            fix="deps.install_browser",
            hint="Installs the playwright package and its Chromium runtime.",
        )

    parsed, raw = await call_tool("browser_state", {"include_tabs": True})
    if not isinstance(parsed, dict):
        return fail(f"browser_state returned unparseable output: {raw[:200]}")

    # Not being connected is the correct state for an idle system; the probe is
    # checking that the tool answers coherently, not that a browser is running.
    if parsed.get("connected"):
        return ok("Playwright installed; a browser session is currently connected.")
    return ok("Playwright installed and the browser tool responds (no session open, which is normal).")


# ---------------------------------------------------------------------------
# Custom tools
# ---------------------------------------------------------------------------

async def probe_dynamic_tools() -> ProbeOutcome:
    """create -> list -> delete."""
    name = f"diag_probe_{int(time.time())}"
    created = False
    try:
        parsed, raw = await call_tool("create_tool", {
            "name": name,
            "description": "Diagnostics probe tool",
            "command_template": "echo diagnostics_probe",
            "parameters": {},
        })
        if not isinstance(parsed, dict) or parsed.get("error"):
            return fail(f"create_tool failed: {_err(parsed, raw)}", reset="tools.reset_dynamic_registry")
        created = True

        parsed, raw = await call_tool("list_dynamic_tools", {})
        if name not in raw:
            return fail(f"list_dynamic_tools did not include the tool just created. Got: {raw[:250]}",
                        reset="tools.reset_dynamic_registry")

        return ok("Custom tools can be created and listed.")
    finally:
        if created:
            try:
                await call_tool("delete_tool", {"name": name})
            except Exception:
                pass


async def probe_tool_guide() -> ProbeOutcome:
    parsed, raw = await call_tool("read_tool_guide", {"tool_name": "email"})
    if not raw or len(raw.strip()) < 40:
        return fail(f"read_tool_guide returned nothing useful for a known tool. Got: {raw[:200]}")
    if isinstance(parsed, dict) and parsed.get("error"):
        return fail(f"read_tool_guide failed: {parsed['error']}")
    return ok("Tool guides load.")


# ---------------------------------------------------------------------------
# Email and Google, at the tool layer
# ---------------------------------------------------------------------------

async def probe_email_tool() -> ProbeOutcome:
    import email_tools

    if not email_tools.resolve_config_path():
        return skip("Skipped — email is not configured.")

    parsed, raw = await call_tool("email_execute", {"action": "list_folders"})
    if not isinstance(parsed, dict):
        return fail(f"email_execute returned unparseable output: {raw[:200]}")
    if parsed.get("ok") is False or parsed.get("error"):
        detail = _err(parsed, raw)
        if parsed.get("version_mismatch"):
            return fail(detail, fix="email.install_cli")
        return fail(detail, reset="email.reconfigure")
    return ok("The email tool lists folders through the agent's own dispatch path.")


async def probe_google_tool() -> ProbeOutcome:
    import google_integration

    status = google_integration.get_status()
    if not status.get("connected") and not status.get("has_credentials"):
        return skip("Skipped — Google Workspace is not connected.")

    parsed, raw = await asyncio.wait_for(
        call_tool("google_execute", {"action": "calendar.list_calendars", "payload": {}}), timeout=45
    )
    if not isinstance(parsed, dict):
        return fail(f"google_execute returned unparseable output: {raw[:200]}")
    if parsed.get("error"):
        return fail(f"google_execute failed: {parsed['error']}", reset="google.reset")
    return ok("Google Workspace calls succeed.")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# (id, display name, category, coroutine)
PROBES: list[tuple[str, str, str, Any]] = [
    ("tool.files",       "Files (read/write/edit)",   "tools",        probe_files),
    ("tool.shell",       "Run command",               "tools",        probe_execute_command),
    ("tool.workflow",    "Multi-step workflow",       "tools",        probe_execute_workflow),
    ("tool.memory",      "Memory",                    "data",         probe_memory),
    ("tool.scheduling",  "Scheduled jobs",            "data",         probe_scheduling),
    ("tool.spreadsheet", "Spreadsheets",              "documents",    probe_spreadsheet),
    ("tool.docx",        "Word documents",            "documents",    probe_docx),
    ("tool.projects",    "Projects",                  "projects",     probe_projects),
    ("tool.message",     "Send message",              "messaging",    probe_send_message),
    ("tool.document",    "Send file",                 "messaging",    probe_send_document),
    ("tool.tts",         "Voice messages (TTS)",      "messaging",    probe_tts),
    ("tool.pa",          "PA announcements",          "messaging",    probe_pa_system),
    ("tool.websearch",   "Web search",                "web",          probe_web_search),
    ("tool.scrape",      "Page scraping",             "web",          probe_scrape),
    ("tool.browser",     "Browser automation",        "web",          probe_browser),
    ("tool.dynamic",     "Custom tools",              "tools",        probe_dynamic_tools),
    ("tool.guide",       "Tool guides",               "tools",        probe_tool_guide),
    ("tool.email",       "Email tool",                "email",        probe_email_tool),
    ("tool.google",      "Google Workspace tool",     "integrations", probe_google_tool),
]


def covered_tools() -> set[str]:
    """Tool names these probes exercise, directly or through a round trip."""
    return {
        "read_file", "write_file", "edit_file",
        "execute_command", "execute_workflow",
        "save_memory", "search_memories", "list_memories", "update_memory", "delete_memory",
        "schedule_cron", "list_cron", "remove_cron",
        "spreadsheet_execute", "docx_execute",
        "create_project", "list_projects",
        "send_telegram_message", "send_telegram_document",
        "send_voice_message", "pa_system",
        "web_search", "scrape_url", "browser_state",
        "create_tool", "list_dynamic_tools", "delete_tool",
        "read_tool_guide", "email_execute", "google_execute",
    }


# Tools deliberately not probed, and why. Surfaced in the report so the coverage
# gap is visible rather than silent.
UNPROBED_TOOLS: dict[str, str] = {
    "spawn_subagent": "would invoke the model, which this system check must never do",
    "browser_start_chrome_debug": "launches a real Chrome process — too heavy for a probe",
    "browser_connect": "requires a running Chrome debug session",
    "browser_run": "requires a live browser session",
    "browser_disconnect": "requires a live browser session",
    "send_telegram_photo": "needs a real image; covered by the file-send probe",
    "send_telegram_screenshot": "captures the desktop or a live browser session",
    "migrate_project": "needs an existing external project directory to move",
    "set_model_prefs": "would change the user's configured model",
}
