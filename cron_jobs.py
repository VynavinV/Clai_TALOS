import asyncio
from datetime import datetime, timezone
from typing import Any

from croniter import croniter

import db
import terminal_tools


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


def _summarize_result(result: dict[str, Any]) -> str:
    if "error" in result:
        return f"error: {result.get('error')}"
    stdout = (result.get("stdout") or "").strip()
    stderr = (result.get("stderr") or "").strip()
    if stderr:
        summary = f"stderr: {stderr}"
    else:
        summary = stdout
    if len(summary) > 500:
        summary = summary[:500] + "..."
    return summary or "(no output)"


async def run_due_jobs() -> list[dict]:
    now_iso = _now_iso()
    due = db.get_due_cron_jobs(now_iso)
    results = []
    for job in due:
        result = await terminal_tools.execute_command(job["command"])
        last_run = _now_iso()
        next_run = _next_run_iso(job["schedule"], datetime.now(timezone.utc))
        summary = _summarize_result(result)
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
            pass
        await asyncio.sleep(interval_seconds)
