import logging
import asyncio
import os
import time
import AI
import db
import model_router
import activity_tracker
import interrupt_hub
import panel_popups

logger = logging.getLogger("talos.core")

GREETINGS = {"hi", "hello", "hey"}

# Active orchestrator runs by user id, so /stop and the panel stop button can
# cancel a run in flight no matter which channel started it.
_ACTIVE_RUNS: dict[int, asyncio.Task] = {}

# Inner agent-turn tasks by user id. Cancelling these lands in the run's own
# cancellation handling (ack message, history bookkeeping) instead of tearing
# the run down mid-send.
_ACTIVE_AGENT_TURNS: dict[int, asyncio.Task] = {}

# Users whose run was cancelled because they sent a new message. The run's
# cancellation branch uses this to acknowledge and keep history coherent
# instead of reporting an unexplained stop.
_INTERRUPTED_BY_USER: set[int] = set()


def stop_user_runs(user_id: int) -> dict:
    """Emergency stop: cancel the user's orchestrator run and its swarm.

    Returns what was stopped so callers can tell the user exactly that.
    """
    agents_stopped = AI.stop_background_subagents(user_id)
    # A confirm the stopped run was waiting on must not swallow a later
    # bare "yes"/"no" chat reply.
    panel_popups.hub.dismiss_pending_confirms()
    task = _ACTIVE_RUNS.get(user_id)
    run_stopped = False
    if task is not None and not task.done():
        task.cancel()
        run_stopped = True
    return {"run_stopped": run_stopped, "agents_stopped": agents_stopped}


def interrupt_user_run(user_id: int) -> dict:
    """Stop the user's active run because they sent a new message.

    The user's fresh message outranks whatever is running: stop the run and its
    background swarm completely. Unlike stop_user_runs, the cancelled run
    knows it was interrupted — it acknowledges, keeps chat history coherent,
    and stays quiet about a "stopped task". The caller should then process the
    new message as a fresh turn once the old one unwinds.
    """
    user_id = int(user_id)
    agents_stopped = AI.stop_background_subagents(user_id)
    # The interrupted run can no longer receive a confirm answer; close any
    # pending confirm so it doesn't swallow the user's next "yes"/"no".
    panel_popups.hub.dismiss_pending_confirms()
    run_stopped = False
    turn_task = _ACTIVE_AGENT_TURNS.get(user_id)
    if turn_task is not None and not turn_task.done():
        _INTERRUPTED_BY_USER.add(user_id)
        turn_task.cancel()
        run_stopped = True
    else:
        task = _ACTIVE_RUNS.get(user_id)
        if task is not None and not task.done():
            _INTERRUPTED_BY_USER.add(user_id)
            task.cancel()
            run_stopped = True
    return {"run_stopped": run_stopped, "agents_stopped": agents_stopped}


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
        # Pause between stopping everything and restarting with the recovery
        # message: gives the provider time to catch up (in-flight model calls
        # cancelled, rate limits settling) before the retry fires.
        "recovery_pause_s": _int_env("TALOS_STUCK_RECOVERY_PAUSE_S", 5, 0),
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


async def _stuck_watchdog(send_func, state: dict, interrupt_queue: asyncio.Queue | None, user_id: int | None = None) -> None:
    """Recovery ladder for a hung run:

    1. Recovery available (retries left, cancellable run): STOP everything —
       the run and its background swarm. core.process_message then pauses
       `recovery_pause_s` (so the provider can catch up), injects a recovery
       message, and restarts the task with a recovery prompt.
    2. Otherwise (image turns, retries spent): inject recovery messages the
       agent sees at its next round boundary, then cancel outright past
       `cancel_after_s` — injections cannot reach an agent blocked inside one
       long await.
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

            run_task = state.get("run_task")
            cancellable = run_task is not None and not run_task.done() and not state.get("cancelled_by_watchdog")
            recovery_available = state.get("retries_left", 0) > 0

            if cancellable and recovery_available:
                # Hard stop: kill the run AND its swarm. The retry path in
                # core.process_message restarts it with a recovery prompt.
                state["cancelled_by_watchdog"] = True
                state["stuck_for_s"] = int(real_activity_age)
                agents_stopped = AI.stop_background_subagents(user_id) if user_id is not None else 0
                logger.warning(
                    "Watchdog stopping run after %ds stuck (cancelled %d background agent(s))",
                    int(real_activity_age),
                    agents_stopped,
                )
                run_task.cancel()
                message = f"No progress for {int(real_activity_age)}s — stopping the task completely"
                if agents_stopped:
                    message += f" and its {agents_stopped} background agent{'s' if agents_stopped != 1 else ''}"
                message += (
                    f". Pausing {cfg['recovery_pause_s']}s for the provider to catch up, then restarting with a recovery message."
                    if cfg["recovery_pause_s"] > 0
                    else ". Restarting with a recovery message."
                )
                await _send_with_optional_voice(send_func, message)
                state["last_activity"] = now
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

            # Message interventions spent and still no progress on a run we
            # can cancel (retries exhausted): stop it for good.
            if cancellable and real_activity_age >= cfg["cancel_after_s"]:
                state["cancelled_by_watchdog"] = True
                agents_stopped = AI.stop_background_subagents(user_id) if user_id is not None else 0
                logger.warning(
                    "Watchdog cancelling run after %ds stuck (interventions exhausted)",
                    int(real_activity_age),
                )
                run_task.cancel()
                await _send_with_optional_voice(
                    send_func,
                    f"The task has been hung for {int(real_activity_age)}s and ignored {state['interventions_sent']} recovery attempts. Stopping it.",
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


async def _acknowledge_user_interrupt(user_id: int, text: str, send_func, handle) -> None:
    """A run was cancelled because the user sent a new message.

    Persists what the interrupted run was about (plus any messages injected
    into it) so the conversation stays coherent for the fresh turn, then
    acknowledges the stop in chat.
    """
    db.add_message(user_id, "user", text)
    try:
        for injected_text in interrupt_hub.take_injected(user_id, handle.queue):
            db.add_message(user_id, "user", injected_text)
    except Exception:
        logger.exception("Could not persist injected messages after interrupt")
    db.add_message(user_id, "assistant", "(Task interrupted by your new message — stopped here.)")
    await _send_with_optional_voice(
        send_func,
        "Stopped the previous task completely. Starting on your new message now.",
    )


async def process_message(user_id: int, text: str, send_func, model_override: str | None = None) -> None:
    """Run a chat turn, registered so an emergency stop can cancel it."""
    run_task = asyncio.current_task()
    _ACTIVE_RUNS[user_id] = run_task
    # A stale interrupt flag (a run that died before consuming it) must not
    # mislabel this run's cancellation later.
    _INTERRUPTED_BY_USER.discard(user_id)
    try:
        await _process_message_impl(user_id, text, send_func, model_override)
    finally:
        if _ACTIVE_RUNS.get(user_id) is run_task:
            _ACTIVE_RUNS.pop(user_id, None)


async def _process_message_impl(user_id: int, text: str, send_func, model_override: str | None = None) -> None:
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
    # Register this run so messages that arrive while we work can stop it
    # (the user's new message outranks the running task) and so /stop works.
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

    def _spawn_turn(prompt_text: str) -> asyncio.Task:
        turn = asyncio.create_task(_run_agent_turn(prompt_text))
        _ACTIVE_AGENT_TURNS[user_id] = turn
        return turn

    try:
        watcher_task = asyncio.create_task(_real_activity_watcher(watchdog_state))
        watchdog_task = asyncio.create_task(_stuck_watchdog(send_func, watchdog_state, handle.queue, user_id=user_id))
        progress_task = asyncio.create_task(_progress_notifier(tracked_send, watchdog_state))
        run_task = _spawn_turn(text)
        watchdog_state["run_task"] = run_task
        try:
            reply = await run_task
        except asyncio.CancelledError:
            if user_id in _INTERRUPTED_BY_USER:
                # The user sent a new message, which outranks this task. Keep
                # history coherent, acknowledge, and let the new turn run.
                _INTERRUPTED_BY_USER.discard(user_id)
                await _acknowledge_user_interrupt(user_id, text, send_func, handle)
                return

            if watchdog_state.get("cancelled_by_watchdog") and watchdog_state.get("retries_left", 0) > 0:
                watchdog_state["retries_left"] -= 1
                watchdog_state["cancelled_by_watchdog"] = False
                watchdog_state["interventions_sent"] = 0
                watchdog_state["last_real_activity"] = time.monotonic()
                cfg = _stuck_settings()
                pause_s = max(0, cfg["recovery_pause_s"])
                logger.warning(
                    "Watchdog-recovered run for user %s; pausing %ds then retrying once with recovery prompt",
                    user_id,
                    pause_s,
                )
                try:
                    await _send_with_optional_voice(
                        send_func,
                        "Recovered from a hung task — everything is stopped."
                        + (
                            f" Pausing {pause_s}s to let the provider catch up, then restarting with instructions to take a different approach."
                            if pause_s > 0
                            else " Restarting it now with instructions to take a different approach."
                        ),
                    )
                    if pause_s > 0:
                        # Give the provider time to settle: in-flight model calls
                        # are cancelled, rate limits recover, sockets drain.
                        await asyncio.sleep(pause_s)
                except asyncio.CancelledError:
                    # The user sent a new message even during the recovery
                    # pause. It still outranks the task.
                    if user_id in _INTERRUPTED_BY_USER:
                        _INTERRUPTED_BY_USER.discard(user_id)
                        await _acknowledge_user_interrupt(user_id, text, send_func, handle)
                        return
                    raise
                # Recovery message the restarted run sees as a system
                # intervention at its first round boundary.
                stuck_for = watchdog_state.get("stuck_for_s")
                recovery_msg = _STUCK_RECOVERY_MESSAGES[0].format(elapsed=stuck_for or "that long")
                try:
                    handle.queue.put_nowait({"text": recovery_msg, "source": "stuck_watchdog"})
                except asyncio.QueueFull:
                    pass
                recovery_prompt = (
                    f"{text}\n\n"
                    "[SYSTEM RECOVERY NOTE] Your previous attempt at this task hung with no progress and was "
                    "stopped by the recovery watchdog. Take a DIFFERENT approach this time: avoid the exact "
                    "command or request that hung (add timeouts, split long steps, or use another method), keep "
                    "the user informed, and finish the task."
                )
                retry_task = _spawn_turn(recovery_prompt)
                watchdog_state["run_task"] = retry_task
                try:
                    reply = await retry_task
                except asyncio.CancelledError:
                    if user_id in _INTERRUPTED_BY_USER:
                        # The user's new message outranks even the recovery
                        # retry: stop, acknowledge, let the new turn run.
                        _INTERRUPTED_BY_USER.discard(user_id)
                        await _acknowledge_user_interrupt(user_id, text, send_func, handle)
                        return
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
        turn = _ACTIVE_AGENT_TURNS.get(user_id)
        if turn is not None and turn.done():
            _ACTIVE_AGENT_TURNS.pop(user_id, None)
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
        watchdog_task = asyncio.create_task(_stuck_watchdog(send_func, watchdog_state, interrupt_queue, user_id=user_id))
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
