import logging
import asyncio
import os
import time
import AI
import db
import model_router
import activity_tracker

logger = logging.getLogger("talos.core")

GREETINGS = {"hi", "hello", "hey"}
_STUCK_CHECK_INTERVAL_S = max(5, int(os.getenv("TALOS_STUCK_CHECK_INTERVAL_S", "10")))
_STUCK_THRESHOLD_S = max(30, int(os.getenv("TALOS_STUCK_THRESHOLD_S", "90")))
_STUCK_MAX_INTERVENTIONS = max(1, int(os.getenv("TALOS_STUCK_MAX_INTERVENTIONS", "2")))

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

    return _tracked_send


async def _stuck_watchdog(send_func, state: dict, interrupt_queue: asyncio.Queue | None) -> None:
    tracker = activity_tracker.get_tracker()
    queue = await tracker.subscribe()
    try:
        while True:
            await asyncio.sleep(_STUCK_CHECK_INTERVAL_S)

            if state["interventions_sent"] >= _STUCK_MAX_INTERVENTIONS:
                continue

            now = time.monotonic()
            real_activity_age = now - state["last_real_activity"]

            if real_activity_age <= _STUCK_THRESHOLD_S:
                continue

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
            state["last_activity"] = time.monotonic()
    finally:
        await tracker.unsubscribe(queue)


async def _real_activity_watcher(state: dict) -> None:
    tracker = activity_tracker.get_tracker()
    queue = await tracker.subscribe()
    try:
        while True:
            evt = await queue.get()
            evt_type = evt.get("type", "")
            if evt_type in {"thinking", "model", "tool", "command", "spawn", "done", "receive"}:
                state["last_real_activity"] = time.monotonic()
    except asyncio.CancelledError:
        pass
    finally:
        await tracker.unsubscribe(queue)


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

    if stripped in GREETINGS:
        await _send_with_optional_voice(send_func, "hello I am Clai TALOS")
        return

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
        reply = await AI.respond(
            user_id=user_id,
            text=text,
            send_func=tracked_send,
            interrupt_event=interrupt_event,
            interrupt_queue=interrupt_queue,
            model_override=model_override,
        )
        await _cancel_task(watchdog_task)
        await _cancel_task(watcher_task)
        if reply and reply.strip():
            await _send_with_optional_voice(send_func, reply, stream=True)
        else:
            await _send_with_optional_voice(
                send_func,
                "I could not produce a usable final response for that request. Please try again or split it into smaller steps.",
            )
    except Exception as error:
        await _cancel_task(watchdog_task)
        await _cancel_task(watcher_task)
        failure = _format_failure_message(error)
        await _send_with_optional_voice(send_func, f"Execution failed. {failure}")


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
        await _cancel_task(watchdog_task)
        await _cancel_task(watcher_task)
        if reply and reply.strip():
            await _send_with_optional_voice(send_func, f"Execution complete. {reply}")
        else:
            await _send_with_optional_voice(send_func, "Execution complete but no summary was generated.")
    except Exception as error:
        await _cancel_task(watchdog_task)
        await _cancel_task(watcher_task)
        failure = _format_failure_message(error)
        await _send_with_optional_voice(send_func, f"Execution failed. {failure}")
