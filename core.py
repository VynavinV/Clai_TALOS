import logging
import AI
import db

logger = logging.getLogger("talos.core")

GREETINGS = {"hi", "hello", "hey"}


async def process_message(user_id: int, text: str, send_func) -> None:
    stripped = text.lower().strip().rstrip("!")
    if stripped == "/clear":
        deleted = db.clear_history(user_id)
        await send_func(f"Chat cleared ({deleted} messages removed). Memories preserved.")
        return
    if stripped in GREETINGS:
        await send_func("hello I am Clai TALOS")
        return

    try:
        reply = await AI.respond(
            user_id=user_id,
            text=text,
            send_func=send_func,
        )
        if reply:
            await send_func(reply)
    except Exception as e:
        err_str = str(e)
        if "rate" in err_str.lower() or "limit" in err_str.lower() or "balance" in err_str.lower():
            await send_func("API rate limit or balance exhausted.")
        elif "API" in err_str or "auth" in err_str.lower() or "key" in err_str.lower():
            await send_func(f"API error: {e}")
        else:
            logger.exception("Error in process_message")
            await send_func(f"Error: {e}")
