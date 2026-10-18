import logging
import asyncio
import os
import time
import AI
import db
import model_router
import activity_tracker
import interrupt_hub

logger = logging.getLogger("talos.core")

GREETINGS = {"hi", "hello", "hey"}


def _int_env(key: str, default: int, lo: int) -> int:
    try:
        return max(lo, int(os.getenv(key, str(default))))
    except (TypeError, ValueError):
        return default


def _stuck_settings() -> dict:
    """Stuck-recovery knobs, read live so Settings changes apply without a restart."""
    return {
        "enabled": os.getenv("TALOS_STUCK_ENABLED", "1") != "0",
        "check_interval_s": _int_env("TALOS_STUCK_CHECK_INTERVAL_S", 10, 5),
        "threshold_s": _int_env("TALOS_STUCK_THRESHOLD_S", 90, 30),
        "max_interventions": _int_env("TALOS_STUCK_MAX_INTERVENTIONS", 2, 1),
        # Escalation: past this much stuck time the run is cancelled outright
        # (message injections alone can't help while blocked in one long await)
        # and, if retries remain, restarted with a recovery prompt.
        "cancel_after_s": _int_env("TALOS_STUCK_CANCEL_S", 600, 60),
        "auto_retries": _int_env("TALOS_STUCK_AUTO_RETRY", 1, 0),
    }


def _progress_settings() -> dict:
    """Telegram keep-in-the-loop knobs, read live from env."""
    return {
        "enabled": os.getenv("TALOS_PROGRESS_ENABLED", "1") != "0",
        "check_interval_s": _int_env("TALOS_PROGRESS_CHECK_INTERVAL_S", 15, 5),
        # Quiet for this long with no user-facing message -> auto status update.
        "silence_threshold_s": _int_env("TALOS_PROGRESS_SILENCE_THRESHOLD_S", 90, 20),
        "min_gap_s": _int_env("TALOS_PROGRESS_MIN_GAP_S", 300, 30),
        "max_updates": _int_env("TALOS_PROGRESS_MAX_AUTO_UPDATES", 60, 0),
    }

_STUCK_RECOVERY_MESSAGES = [
    "SYSTEM INTERVENTION: You have been stuck with no progress for {elapsed}s. Whatever command or operation you're waiting on is likely hung. STOP waiting, try a different approach, and inform the user.",
    "SYSTEM INTERVENTION: Still stuck after {elapsed}s. The current approach is not working. ABANDON it immediately. Try an alternative method or ask the user for help.",
]


async def _send_with_optional_voice(
    send_func,
    message: str = "",
    voice: bool = False,
    photo_path: str | None = None,
    caption: str = "",
    stream: bool = False,
) -> None:
    try:
        if photo_path:
            await send_func(message, photo_path=photo_path, caption=caption)
        elif voice:
            await send_func(message, voice=True)
        else:
            await send_func(message, stream=stream)
    except TypeError:
        await send_func(message)


async def _cancel_task(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _build_activity_send(send_func, state: dict):
    async def _tracked_send(
        message: str = "",
        voice: bool = False,
        photo_path: str | None = None,
        caption: str = "",
        stream: bool = False,
    ) -> None:
        await _send_with_optional_voice(
            send_func,
            message,
            voice=voice,
            photo_path=photo_path,
            caption=caption,
            stream=stream,
        )
        state["last_activity"] = time.monotonic()
        # Anything the user actually received resets the progress notifier's
        # silence window — no point in an auto-status right after a real one.
        state["last_user_facing"] = time.monotonic()

    return _tracked_send


async def _stuck_watchdog(send_func, state: dict, interrupt_queue: asyncio.Queue | None) -> None:
    """Recovery ladder for a hung run:

    1. Inject recovery messages the agent sees at its next round boundary.
    2. If it stays stuck far longer (cancel_after_s), cancel the whole run —
       injections alone cannot reach an agent that is blocked inside a single
       long await. core.process_message then optionally restarts the task once
       with a recovery prompt.
    """
    try:
        while True:
            cfg = _stuck_settings()
            await asyncio.sleep(cfg["check_interval_s"])

            if not cfg["enabled"]:
                continue

            now = time.monotonic()
            real_activity_age = now - state["last_real_activity"]

            if real_activity_age <= cfg["threshold_s"]:
                continue

            if state["interventions_sent"] < cfg["max_interventions"]:
                elapsed = int(real_activity_age)
                idx = state["interventions_sent"] % len(_STUCK_RECOVERY_MESSAGES)
                recovery_msg = _STUCK_RECOVERY_MESSAGES[idx].format(elapsed=elapsed)
                state["interventions_sent"] += 1

                if interrupt_queue:
                    try:
                        interrupt_queue.put_nowait({"text": recovery_msg, "source": "stuck_watchdog"})
                    except asyncio.QueueFull:
                        pass

                await _send_with_optional_voice(send_func, f"Detected a hang ({elapsed}s no progress). Injecting recovery — trying to unstick myself.")
                state["last_activity"] = now
                continue

            # All message interventions spent and still no progress. Escalate.
            if real_activity_age >= cfg["cancel_after_s"] and not state.get("cancelled_by_watchdog"):
                run_task = state.get("run_task")
                if run_task is not None and not run_task.done():
                    state["cancelled_by_watchdog"] = True
                    logger.warning(
                        "Watchdog cancelling run after %ds stuck (interventions exhausted)",
                        int(real_activity_age),
                    )
                    run_task.cancel()
                    await _send_with_optional_voice(
                        send_func,
                        f"The task has been hung for {int(real_activity_age)}s and ignored {state['interventions_sent']} recovery attempts. "
                        + ("Restarting it now with a fresh approach." if state.get("retries_left", 0) > 0 else "Stopping it."),
                    )
    except asyncio.CancelledError:
        pass


# Events that make a useful one-line "what is it doing right now" summary.
# Streamed model output is deliberately excluded — it is too chatty and not a
# meaningful milestone for the user.
_SUMMARY_EVENT_TYPES = {"thinking", "model", "tool", "command", "result", "spawn", "done", "error"}


def _summarize_event(evt: dict) -> str:
    detail = str(evt.get("detail") or "").strip()
    label = str(evt.get("label") or "").strip()
    text = detail or label
    if len(text) > 110:
        text = text[:107].rstrip() + "..."
    return text


async def _real_activity_watcher(state: dict) -> None:
    tracker = activity_tracker.get_tracker()
    queue = await tracker.subscribe()
    try:
        while True:
            evt = await queue.get()
            evt_type = evt.get("type", "")
            if evt_type in {"thinking", "model", "tool", "command", "spawn", "done", "receive", "stream", "reasoning", "result"}:
                state["last_real_activity"] = time.monotonic()
                if evt_type in _SUMMARY_EVENT_TYPES:
                    state["last_event_desc"] = _summarize_event(evt)
    except asyncio.CancelledError:
        pass
    finally:
        await tracker.unsubscribe(queue)


def _progress_due(state: dict, cfg: dict, now: float) -> bool:
    """Pure decision: should an automatic progress update be sent right now?"""
    if not cfg["enabled"] or cfg["max_updates"] <= 0:
        return False
    if state.get("auto_updates_sent", 0) >= cfg["max_updates"]:
        return False
    last_user_msg = state.get("last_user_facing", state.get("started_at", now))
    quiet_for = now - last_user_msg
    since_last_auto = now - state.get("last_auto_update", 0.0)
    if quiet_for < cfg["silence_threshold_s"]:
        return False
    if since_last_auto < cfg["min_gap_s"]:
        return False
    return True


async def _progress_notifier(send_func, state: dict) -> None:
    """Keep the user in the loop on long tasks.

    The model is capped in how many Telegram updates it may send, and small
    models forget to send any at all. This runs alongside the task and, when
    nothing has been delivered to the user for `silence_threshold_s`, sends
    an automatic one-liner describing what the agent is doing right now
    (from live activity events). Stops after `max_updates` automatic updates
    or whenever the run ends.
    """
    try:
        while True:
            cfg = _progress_settings()
            await asyncio.sleep(cfg["check_interval_s"])

            now = time.monotonic()
            if not _progress_due(state, cfg, now):
                continue

            minutes = max(1, round((now - state.get("started_at", now)) / 60))
            desc = str(state.get("last_event_desc") or "").strip()
            if desc:
                message = f"[auto-status] Still working — {minutes} min in. Latest: {desc}. I'll message you when there's real news."
            else:
                message = f"[auto-status] Still working — {minutes} min in. I'll message you when there's real news."

            state["auto_updates_sent"] = state.get("auto_updates_sent", 0) + 1
            state["last_auto_update"] = now
            # Counts as contact with the user: reset the silence window.
            state["last_user_facing"] = now
            try:
                await _send_with_optional_voice(send_func, message)
            except Exception:
                logger.exception("Progress update send failed")
    except asyncio.CancelledError:
        pass


def _format_failure_message(error: Exception) -> str:
    err_str = str(error)
    lowered = err_str.lower()
    if "rate" in lowered or "limit" in lowered or "balance" in lowered:
        return "API rate limit or balance exhausted."
    if "api" in lowered or "auth" in lowered or "key" in lowered:
        return "API error: authentication or key issue."
    logger.exception("Error in process_message")
    return "An internal error occurred. Check logs for details."


async def process_message(user_id: int, text: str, send_func, model_override: str | None = None) -> None:
    stripped = text.lower().strip().rstrip("!")

    if stripped == "/clear":
        deleted = db.clear_history(user_id)
        await _send_with_optional_voice(send_func, f"Chat cleared ({deleted} messages removed). Memories preserved.")
        return

    if stripped == "/raid":
        enabled = db.set_raid_mode(user_id, not db.get_raid_mode(user_id))
        if enabled:
            await _send_with_optional_voice(
                send_func,
                "RAID MODE ON. Subagent limits lifted — I'll build a specialized swarm for large "
                "tasks. Warning: token heavy. Toggle again with /raid.",
            )
        else:
            await _send_with_optional_voice(send_func, "RAID MODE OFF. Back to normal limits.")
        return

    if stripped in GREETINGS:
        await _send_with_optional_voice(send_func, "hello I am Clai TALOS")
        return

    watchdog_task: asyncio.Task | None = None
    watcher_task: asyncio.Task | None = None
    progress_task: asyncio.Task | None = None
    now = time.monotonic()
    watchdog_state = {
        "started_at": now,
        "last_activity": now,
        "last_real_activity": now,
        "last_user_facing": now,
        "last_auto_update": 0.0,
        "auto_updates_sent": 0,
        "last_event_desc": "",
        "interventions_sent": 0,
        "retries_left": _stuck_settings()["auto_retries"],
        "run_task": None,
        "cancelled_by_watchdog": False,
    }
    # Register this run so messages that arrive while we work are injected
    # between the agent's model calls instead of queueing behind the task.
    handle = interrupt_hub.register(user_id)
    interrupt_event = asyncio.Event()
    tracked_send = _build_activity_send(send_func, watchdog_state)

    async def _run_agent_turn(prompt_text: str) -> str:
        return await AI.respond(
            user_id=user_id,
            text=prompt_text,
            send_func=tracked_send,
            interrupt_event=interrupt_event,
            interrupt_queue=handle.queue,
            model_override=model_override,
        )

    try:
        watcher_task = asyncio.create_task(_real_activity_watcher(watchdog_state))
        watchdog_task = asyncio.create_task(_stuck_watchdog(send_func, watchdog_state, handle.queue))
        progress_task = asyncio.create_task(_progress_notifier(tracked_send, watchdog_state))
        run_task = asyncio.create_task(_run_agent_turn(text))
        watchdog_state["run_task"] = run_task
        try:
            reply = await run_task
        except asyncio.CancelledError:
            if watchdog_state.get("cancelled_by_watchdog") and watchdog_state.get("retries_left", 0) > 0:
                watchdog_state["retries_left"] -= 1
                watchdog_state["cancelled_by_watchdog"] = False
                watchdog_state["interventions_sent"] = 0
                watchdog_state["last_real_activity"] = time.monotonic()
                logger.warning("Watchdog-recovered run for user %s; retrying once with recovery prompt", user_id)
                await _send_with_optional_voice(
                    send_func,
                    "Recovered from a hung task — restarting it now with instructions to take a different approach.",
                )
                recovery_prompt = (
                    f"{text}\n\n"
                    "[SYSTEM RECOVERY NOTE] Your previous attempt at this task hung and was cancelled by the "
                    "recovery watchdog. Take a DIFFERENT approach this time: avoid the exact command or request "
                    "that hung (add timeouts, split long steps, or use another method), keep the user informed, "
                    "and finish the task."
                )
                retry_task = asyncio.create_task(_run_agent_turn(recovery_prompt))
                watchdog_state["run_task"] = retry_task
                try:
                    reply = await retry_task
                except asyncio.CancelledError:
                    await _send_with_optional_voice(
                        send_func,
                        "The task stayed hung even after a recovery restart and was stopped. "
                        "Try a narrower request, or check the logs for what is blocking it.",
                    )
                    return
            else:
                # Either the user stopped the task (/stop, shutdown) or it was
                # cancelled with no retries left. Both mean: unfinished work.
                await _send_with_optional_voice(
                    send_func,
                    "The task was stopped before it finished. If you didn't stop it yourself, "
                    "it stayed hung after recovery attempts — check the logs, or try a narrower request.",
                )
                return

        if AI.was_already_sent(reply):
            # The agent answered through send_telegram_message; the user has it.
            logger.info("Final reply already delivered via message tool; not resending")
        elif reply and reply.strip():
            await _send_with_optional_voice(send_func, reply, stream=True)
        else:
            await _send_with_optional_voice(
                send_func,
                "I could not produce a usable final response for that request. Please try again or split it into smaller steps.",
            )
    except Exception as error:
        failure = _format_failure_message(error)
        await _send_with_optional_voice(send_func, f"Execution failed. {failure}")
    finally:
        interrupt_hub.unregister(user_id, handle)
        await _cancel_task(watchdog_task)
        await _cancel_task(watcher_task)
        await _cancel_task(progress_task)


async def process_image_message(user_id: int, text: str, image_b64: str, send_func) -> None:
    image_model = db.get_image_model(user_id)
    provider, _ = model_router.resolve_model(image_model)
    if not model_router._provider_enabled(provider):
        cfg = model_router._PROVIDERS.get(provider, {})
        env_key = cfg.get("env_key", "API_KEY")
        await _send_with_optional_voice(
            send_func,
            f"Image model \"{image_model}\" requires provider \"{provider}\", but {env_key} is not set. Add your API key in Settings to use image features.",
        )
        return

    await _send_with_optional_voice(send_func, "Got it. Analyzing image...")

    watchdog_task: asyncio.Task | None = None
    watcher_task: asyncio.Task | None = None
    now = time.monotonic()
    watchdog_state = {
        "started_at": now,
        "last_activity": now,
        "last_real_activity": now,
        "interventions_sent": 0,
    }
    interrupt_event = asyncio.Event()
    interrupt_queue = asyncio.Queue(maxsize=10)
    tracked_send = _build_activity_send(send_func, watchdog_state)
    try:
        watcher_task = asyncio.create_task(_real_activity_watcher(watchdog_state))
        watchdog_task = asyncio.create_task(_stuck_watchdog(send_func, watchdog_state, interrupt_queue))
        reply = await AI.respond_with_image(
            user_id=user_id,
            text=text,
            image_b64=image_b64,
            send_func=tracked_send,
        )
        if reply and reply.strip():
            await _send_with_optional_voice(send_func, f"Execution complete. {reply}")
        else:
            await _send_with_optional_voice(send_func, "Execution complete but no summary was generated.")
    except Exception as error:
        failure = _format_failure_message(error)
        await _send_with_optional_voice(send_func, f"Execution failed. {failure}")
    finally:
        await _cancel_task(watchdog_task)
        await _cancel_task(watcher_task)
