import logging
import asyncio
import os
import AI
import db
import model_router

logger = logging.getLogger("talos.core")

GREETINGS = {"hi", "hello", "hey"}
_PROGRESS_SILENCE_THRESHOLD_S = max(20, int(os.getenv("TALOS_PROGRESS_SILENCE_THRESHOLD_S", "45")))
_PROGRESS_MIN_GAP_S = max(45, int(os.getenv("TALOS_PROGRESS_MIN_GAP_S", "120")))
_PROGRESS_CHECK_INTERVAL_S = max(5, int(os.getenv("TALOS_PROGRESS_CHECK_INTERVAL_S", "10")))
_PROGRESS_MAX_AUTO_UPDATES = max(0, int(os.getenv("TALOS_PROGRESS_MAX_AUTO_UPDATES", "0")))
_PROGRESS_MESSAGES = [
    "Quick update: still executing. I will send the next meaningful result.",
    "Still running after {elapsed}s. No action needed from you right now.",
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
        state["last_activity"] = asyncio.get_running_loop().time()

    return _tracked_send


async def _progress_loop(send_func, state: dict) -> None:
    while True:
        await asyncio.sleep(_PROGRESS_CHECK_INTERVAL_S)

        if state["auto_updates_sent"] >= _PROGRESS_MAX_AUTO_UPDATES:
            continue

        now = asyncio.get_running_loop().time()
        if (now - state["last_activity"]) < _PROGRESS_SILENCE_THRESHOLD_S:
            continue
        if (now - state["last_auto_update"]) < _PROGRESS_MIN_GAP_S:
            continue

        elapsed = int(now - state["started_at"])
        template = _PROGRESS_MESSAGES[state["auto_updates_sent"] % len(_PROGRESS_MESSAGES)]
        message = template.format(elapsed=elapsed)
        await _send_with_optional_voice(send_func, message)

        sent_at = asyncio.get_running_loop().time()
        state["last_activity"] = sent_at
        state["last_auto_update"] = sent_at
        state["auto_updates_sent"] += 1


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

    progress_task: asyncio.Task | None = None
    loop = asyncio.get_running_loop()
    progress_state = {
        "started_at": loop.time(),
        "last_activity": loop.time(),
        "last_auto_update": 0.0,
        "auto_updates_sent": 0,
    }
    tracked_send = _build_activity_send(send_func, progress_state)
    try:
        if _PROGRESS_MAX_AUTO_UPDATES > 0:
            progress_task = asyncio.create_task(_progress_loop(send_func, progress_state))
        reply = await AI.respond(
            user_id=user_id,
            text=text,
            send_func=tracked_send,
            model_override=model_override,
        )
        await _cancel_task(progress_task)
        if reply:
            await _send_with_optional_voice(send_func, reply, stream=True)
        else:
            await _send_with_optional_voice(send_func, "Done.")
    except Exception as error:
        await _cancel_task(progress_task)
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
    
    progress_task: asyncio.Task | None = None
    loop = asyncio.get_running_loop()
    progress_state = {
        "started_at": loop.time(),
        "last_activity": loop.time(),
        "last_auto_update": 0.0,
        "auto_updates_sent": 0,
    }
    tracked_send = _build_activity_send(send_func, progress_state)
    try:
        if _PROGRESS_MAX_AUTO_UPDATES > 0:
            progress_task = asyncio.create_task(_progress_loop(send_func, progress_state))
        reply = await AI.respond_with_image(
            user_id=user_id,
            text=text,
            image_b64=image_b64,
            send_func=tracked_send,
        )
        await _cancel_task(progress_task)
        if reply:
            await _send_with_optional_voice(send_func, f"Execution complete. {reply}")
        else:
            await _send_with_optional_voice(send_func, "Execution complete.")
    except Exception as error:
        await _cancel_task(progress_task)
        failure = _format_failure_message(error)
        await _send_with_optional_voice(send_func, f"Execution failed. {failure}")
