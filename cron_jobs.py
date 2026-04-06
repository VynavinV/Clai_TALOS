import asyncio
import logging
import os
from datetime import datetime, timezone

from croniter import croniter

import db
import terminal_tools

logger = logging.getLogger("talos.cron")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _next_run_iso(schedule: str, base: datetime | None = None) -> str:
    base_time = base or datetime.now(timezone.utc)
    iterator = croniter(schedule, base_time)
    next_time = iterator.get_next(datetime)
    if next_time.tzinfo is None:
        next_time = next_time.replace(tzinfo=timezone.utc)
    return next_time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def schedule_job(
    user_id: int,
    name: str,
    schedule: str,
    command: str,
    timezone_name: str = "UTC",
) -> dict:
    croniter(schedule, datetime.now(timezone.utc))
    next_run = _next_run_iso(schedule)
    job_id = db.add_cron_job(user_id, name, schedule, command, timezone_name, next_run)
    return {
        "id": job_id,
        "name": name,
        "schedule": schedule,
        "command": command,
        "timezone": timezone_name,
        "next_run": next_run,
    }


def list_jobs(user_id: int) -> list[dict]:
    return db.list_cron_jobs(user_id)


def remove_job(user_id: int, job_id: int) -> bool:
    return db.remove_cron_job(user_id, job_id)


def _is_self_prompt(command: str) -> bool:
    prefix = command.strip().lower()
    return prefix.startswith("self:") or prefix.startswith("prompt:")


def _make_send_func(user_id: int):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not set — cron self-prompts cannot deliver messages")
        async def noop(*_, **__):
            pass
        return noop

    from telegram import Bot
    bot = Bot(token=token)

    async def send_func(msg: str = "", voice: bool = False, photo_path: str | None = None, caption: str = "", stream: bool = False, **_):
        text = (msg or "").strip()
        if not text:
            return
        for i in range(0, len(text), 4096):
            chunk = text[i:i + 4096]
            try:
                await bot.send_message(chat_id=user_id, text=chunk)
            except Exception as exc:
                logger.warning("Cron send failed for %d: %s", user_id, exc)
                break

    return send_func


async def _run_self_prompt(user_id: int, job_name: str, command: str) -> str:
    prefix_len = 0
    for pfx in ("self:", "prompt:"):
        if command.strip().lower().startswith(pfx):
            prefix_len = len(pfx)
            break

    prompt_text = command.strip()[prefix_len:].strip()
    if not prompt_text:
        prompt_text = command.strip()

    import core
    send_func = _make_send_func(user_id)

    try:
        await core.process_message(user_id, prompt_text, send_func)
        return f"Self-prompt executed: {prompt_text[:100]}"
    except Exception as exc:
        logger.exception("Self-prompt failed for job '%s'", job_name)
        return f"Self-prompt error: {exc}"


async def _run_shell_command(command: str) -> dict:
    executor = terminal_tools.get_executor()
    return await executor.execute(
        command,
        timeout=120,
        require_confirmation=False,
    )


def _summarize_result(result: dict) -> str:
    if "error" in result:
        return f"error: {result.get('error')}"
    stdout = (result.get("stdout") or "").strip()
    stderr = (result.get("stderr") or "").strip()
    if stderr:
        summary = f"stderr: {stderr}"
    else:
        summary = stdout
    if len(summary) > 4000:
        summary = summary[:4000] + "..."
    return summary or "(no output)"


async def run_due_jobs() -> list[dict]:
    now_iso = _now_iso()
    due = db.get_due_cron_jobs(now_iso)
    results = []
    for job in due:
        last_run = _now_iso()
        next_run = _next_run_iso(job["schedule"], datetime.now(timezone.utc))

        if _is_self_prompt(job["command"]):
            summary = await _run_self_prompt(job["user_id"], job["name"], job["command"])
        else:
            try:
                result = await _run_shell_command(job["command"])
                summary = _summarize_result(result)
            except Exception as exc:
                summary = f"error: {exc}"

        db.update_cron_run(job["id"], last_run, next_run, summary)
        results.append({
            "id": job["id"],
            "name": job["name"],
            "command": job["command"],
            "last_run": last_run,
            "next_run": next_run,
            "result": summary,
        })

    return results


async def cron_loop(stop_event: asyncio.Event | None = None, interval_seconds: int = 30) -> None:
    while True:
        if stop_event and stop_event.is_set():
            return
        try:
            await run_due_jobs()
        except Exception:
            logger.exception("Error in cron_loop")
        await asyncio.sleep(interval_seconds)
