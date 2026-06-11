import re
import os
import asyncio
import tempfile
import logging
import unicodedata
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.error import RetryAfter, TimedOut, NetworkError, BadRequest
from telegram.ext import ContextTypes, Application, MessageHandler, filters, CommandHandler, CallbackQueryHandler
import AI
import db
import core
import model_router

HELP_TEXT = (
    "Clai TALOS - Personal AI Assistant\n\n"
    "Tools I can use:\n"
    "- Web search & URL scraping\n"
    "- Terminal command execution\n"
    "- File read/write operations\n"
    "- Email (via Google Apps Script)\n"
    "- Google Sheets & Docs\n"
    "- Presentations\n"
    "- Voice messages\n"
    "- Image analysis\n"
    "- Scheduled tasks (cron)\n"
    "- Memory (I remember things about you)\n"
    "- Browser automation\n"
    "- Subagent spawning for complex tasks\n\n"
    "Commands:\n"
    "/start - Start or restart\n"
    "/model - Change AI model\n"
    "/speed - Set response speed (quick|fast|normal)\n"
    "/reasoning - Toggle deep reasoning (on|off)\n"
    "/fast - Use Cerebras for next message\n"
    "/clear - Clear chat history\n"
    "/help - Show this message\n"
    "Dashboard: http://localhost:8080"
)

logger = logging.getLogger("talos.handlers")

_whisper_model = None
_chat_send_locks: dict[int, asyncio.Lock] = {}
_user_process_locks: dict[int, asyncio.Lock] = {}

_STREAM_MIN_CHARS = max(60, int(os.getenv("TALOS_STREAM_MIN_CHARS", "120")))
_STREAM_MAX_EDITS = max(4, int(os.getenv("TALOS_STREAM_MAX_EDITS", "18")))
_STREAM_EDIT_DELAY_S = max(0.08, float(os.getenv("TALOS_STREAM_EDIT_DELAY_S", "0.16")))

_LOW_VALUE_PROGRESS_RE = re.compile(
    r"^(?:ok(?:ay)?|got it|on it|working on it|one sec|one second|sure|understood)[.!]?$",
    re.IGNORECASE,
)
_LEADING_ACK_RE = re.compile(
    r"^(?:ok(?:ay)?|got it|on it|sure|understood|sounds good)[.!]?\s+",
    re.IGNORECASE,
)

_ONBOARDING_STEP_KEY = "onboarding_step"
_ONBOARDING_NAME_KEY = "onboarding_name"


def _strip_markdown(text: str) -> str:
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'_(.+?)_', r'\1', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    text = re.sub(r'```.+?```', lambda m: m.group(0).strip('`'), text, flags=re.DOTALL)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*[-*+]\s+', '- ', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)
    return text.strip()


def _get_chat_lock(chat_id: int) -> asyncio.Lock:
    lock = _chat_send_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _chat_send_locks[chat_id] = lock
    return lock


def _get_user_process_lock(user_id: int) -> asyncio.Lock:
    lock = _user_process_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_process_locks[user_id] = lock
    return lock


def _sanitize_outgoing(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text or "")
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(
        r"<toolcall\b[^>]*>.*?(?:</toolcall>|$)",
        "",
        normalized,
        flags=re.IGNORECASE | re.DOTALL,
    )
    normalized = re.sub(
        r"<argkey\b[^>]*>\s*[^<]*\s*</argkey>\s*<argvalue\b[^>]*>.*?</argvalue>",
        "",
        normalized,
        flags=re.IGNORECASE | re.DOTALL,
    )
    normalized = re.sub(
        r"<argvalue\b[^>]*>.*?</argvalue>",
        "",
        normalized,
        flags=re.IGNORECASE | re.DOTALL,
    )
    normalized = re.sub(r"</?(?:argkey|argvalue|toolcall)\b[^>]*>", "", normalized, flags=re.IGNORECASE)
    cleaned = []
    for ch in normalized:
        if ch in ("\n", "\t"):
            cleaned.append(ch)
            continue
        if ch.isprintable():
            cleaned.append(ch)
    out = "".join(cleaned)
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = out.strip()
    out = _LEADING_ACK_RE.sub("", out).strip()
    if _LOW_VALUE_PROGRESS_RE.match(out):
        return ""
    return out


def _make_send_func(chat):
    async def send_func(
        msg: str = "",
        voice: bool = False,
        photo_path: str | None = None,
        caption: str = "",
        stream: bool = False,
    ) -> None:
        cleaned = _strip_markdown(msg or "")
        cleaned_caption = _strip_markdown(caption or "")
        lock = _get_chat_lock(chat.id)
        try:
            async with lock:
                if photo_path:
                    resolved_path = os.path.expanduser(str(photo_path).strip())
                    if not os.path.isabs(resolved_path):
                        resolved_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), resolved_path)

                    if not os.path.isfile(resolved_path):
                        await _safe_send(chat, f"Could not send image. File not found: {resolved_path}", locked=True)
                        return

                    with open(resolved_path, 'rb') as photo_file:
                        photo_caption = cleaned_caption or cleaned or None
                        await _send_photo_with_retry(chat, photo_file, caption=photo_caption)
                    return

                if voice:
                    import voice as v
                    audio_path = v.text_to_speech(msg)
                    if audio_path:
                        with open(audio_path, 'rb') as audio_file:
                            await _send_voice_with_retry(chat, audio_file)
                        v.cleanup_audio_file(audio_path)
                        return

                if cleaned:
                    sent = False
                    if stream:
                        sent = await _stream_send(chat, cleaned, locked=True)
                    else:
                        sent = await _safe_send(chat, cleaned, locked=True)

                    # Avoid silent final replies when sanitization strips everything.
                    if stream and not sent:
                        await _send_message_with_retry(
                            chat,
                            "I could not produce a clear final response for that request. Please try again.",
                        )
        except Exception:
            logger.exception("Failed to send message to Telegram")
    return send_func


async def _fetch_models() -> list[str]:
    try:
        return AI.list_models()
    except Exception:
        return list(model_router.get_all_model_aliases().values())


def _model_keyboard(current: str, models: list[str]) -> InlineKeyboardMarkup:
    buttons = []
    for model_id in models:
        label = f"{'✓ ' if model_id == current else ''}{model_id}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"model:{model_id}")])
    return InlineKeyboardMarkup(buttons)


def _performance_keyboard(speed_mode: str, reasoning_enabled: bool) -> InlineKeyboardMarkup:
    mode = str(speed_mode or "normal").lower()
    buttons = [
        [
            InlineKeyboardButton(f"{'✓ ' if mode == 'quick' else ''}Quick", callback_data="speed:quick"),
            InlineKeyboardButton(f"{'✓ ' if mode == 'fast' else ''}Fast", callback_data="speed:fast"),
            InlineKeyboardButton(f"{'✓ ' if mode == 'normal' else ''}Normal", callback_data="speed:normal"),
        ],
        [
            InlineKeyboardButton(f"{'✓ ' if reasoning_enabled else ''}Reasoning On", callback_data="reasoning:on"),
            InlineKeyboardButton(f"{'✓ ' if not reasoning_enabled else ''}Reasoning Off", callback_data="reasoning:off"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def _performance_status_text(speed_mode: str, reasoning_enabled: bool) -> str:
    mode = str(speed_mode or "normal").capitalize()
    reasoning = "On" if reasoning_enabled else "Off"
    return (
        f"Speed mode: **{mode}**\n"
        f"Reasoning: **{reasoning}**\n\n"
        "Use /speed quick|fast|normal or /reasoning on|off"
    )


def _parse_reasoning_value(value: str, current: bool) -> bool | None:
    lowered = str(value or "").strip().lower()
    if lowered in {"on", "true", "1", "yes", "y"}:
        return True
    if lowered in {"off", "false", "0", "no", "n"}:
        return False
    if lowered == "toggle":
        return not current
    return None


async def _ensure_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    user = update.effective_user
    if not user:
        return True

    uid = user.id
    if db.has_user_profile(uid):
        context.user_data.pop(_ONBOARDING_STEP_KEY, None)
        context.user_data.pop(_ONBOARDING_NAME_KEY, None)
        return False

    step = context.user_data.get(_ONBOARDING_STEP_KEY)

    if step is None:
        context.user_data[_ONBOARDING_STEP_KEY] = "name"
        await update.message.reply_text("Welcome to Clai TALOS. Before we start, what's your name?")
        return True

    if step == "name":
        name = text.strip()
        if len(name) < 2:
            await update.message.reply_text("Please enter a valid name (at least 2 characters).")
            return True
        if len(name) > 60:
            await update.message.reply_text("Name is too long. Keep it under 60 characters.")
            return True

        context.user_data[_ONBOARDING_NAME_KEY] = name
        context.user_data[_ONBOARDING_STEP_KEY] = "about"
        await update.message.reply_text("Nice to meet you. Tell me a bit about you (role, goals, or what you want help with).")
        return True

    if step == "about":
        about = text.strip()
        if len(about) < 5:
            await update.message.reply_text("Please share a bit more detail (at least 5 characters).")
            return True

        name = context.user_data.get(_ONBOARDING_NAME_KEY) or user.full_name or "User"
        db.upsert_user_profile(uid, name, about[:500])
        context.user_data.pop(_ONBOARDING_STEP_KEY, None)
        context.user_data.pop(_ONBOARDING_NAME_KEY, None)
        await update.message.reply_text(f"Profile saved, {name}. You're all set. Ask me anything.")
        return True

    context.user_data[_ONBOARDING_STEP_KEY] = "name"
    await update.message.reply_text("Let's reset onboarding. What's your name?")
    return True


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if db.has_user_profile(uid):
        profile = db.get_user_profile(uid) or {}
        name = profile.get("name") or update.effective_user.full_name or "there"
        await update.message.reply_text(f"Welcome back, {name}. Ask me anything.")
        return

    context.user_data[_ONBOARDING_STEP_KEY] = "name"
    context.user_data.pop(_ONBOARDING_NAME_KEY, None)
    await update.message.reply_text("Welcome to Clai TALOS. What's your name?")


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not db.has_user_profile(uid):
        context.user_data[_ONBOARDING_STEP_KEY] = "name"
        await update.message.reply_text("Please complete onboarding first. What's your name?")
        return
    current = db.get_model(uid)
    models = await _fetch_models()
    await update.message.reply_text(
        f"Current model: **{current}**\nSelect a model:",
        reply_markup=_model_keyboard(current, models),
        parse_mode="Markdown",
    )


async def cmd_speed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not db.has_user_profile(uid):
        context.user_data[_ONBOARDING_STEP_KEY] = "name"
        await update.message.reply_text("Please complete onboarding first. What's your name?")
        return

    current_mode = db.get_speed_mode(uid)
    current_reasoning = db.get_reasoning_enabled(uid)

    if context.args:
        requested = str(context.args[0]).strip().lower()
        if requested not in {"quick", "fast", "normal"}:
            await update.message.reply_text(
                "Usage: /speed quick|fast|normal\n"
                + _performance_status_text(current_mode, current_reasoning),
                parse_mode="Markdown",
            )
            return

        new_mode = db.set_speed_mode(uid, requested)
        current_reasoning = db.get_reasoning_enabled(uid)
        await update.message.reply_text(
            _performance_status_text(new_mode, current_reasoning),
            reply_markup=_performance_keyboard(new_mode, current_reasoning),
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(
        _performance_status_text(current_mode, current_reasoning),
        reply_markup=_performance_keyboard(current_mode, current_reasoning),
        parse_mode="Markdown",
    )


async def cmd_reasoning(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not db.has_user_profile(uid):
        context.user_data[_ONBOARDING_STEP_KEY] = "name"
        await update.message.reply_text("Please complete onboarding first. What's your name?")
        return

    current_mode = db.get_speed_mode(uid)
    current_reasoning = db.get_reasoning_enabled(uid)

    if context.args:
        parsed = _parse_reasoning_value(str(context.args[0]), current_reasoning)
        if parsed is None:
            await update.message.reply_text(
                "Usage: /reasoning on|off|toggle\n"
                + _performance_status_text(current_mode, current_reasoning),
                parse_mode="Markdown",
            )
            return

        current_reasoning = db.set_reasoning_enabled(uid, parsed)

    await update.message.reply_text(
        _performance_status_text(current_mode, current_reasoning),
        reply_markup=_performance_keyboard(current_mode, current_reasoning),
        parse_mode="Markdown",
    )


async def callback_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not db.has_user_profile(uid):
        await update.callback_query.answer("Complete onboarding first.", show_alert=True)
        return
    query = update.callback_query
    await query.answer()
    if not query.data or not query.data.startswith("model:"):
        return
    model_id = query.data.split(":", 1)[1]
    models = await _fetch_models()
    if model_id not in models:
        await query.edit_message_text("Unknown model.")
        return
    db.set_model(update.effective_user.id, model_id)
    await query.edit_message_text(
        f"Model set to **{model_id}**",
        reply_markup=_model_keyboard(model_id, models),
        parse_mode="Markdown",
    )


async def callback_speed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    query = update.callback_query
    await query.answer()

    if not db.has_user_profile(uid):
        await query.edit_message_text("Complete onboarding first.")
        return

    mode = "normal"
    if query.data and ":" in query.data:
        mode = query.data.split(":", 1)[1].strip().lower()
    if mode not in {"quick", "fast", "normal"}:
        mode = db.get_speed_mode(uid)

    mode = db.set_speed_mode(uid, mode)
    reasoning_enabled = db.get_reasoning_enabled(uid)
    await query.edit_message_text(
        _performance_status_text(mode, reasoning_enabled),
        reply_markup=_performance_keyboard(mode, reasoning_enabled),
        parse_mode="Markdown",
    )


async def callback_reasoning(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    query = update.callback_query
    await query.answer()

    if not db.has_user_profile(uid):
        await query.edit_message_text("Complete onboarding first.")
        return

    current_mode = db.get_speed_mode(uid)
    current_reasoning = db.get_reasoning_enabled(uid)

    requested = "toggle"
    if query.data and ":" in query.data:
        requested = query.data.split(":", 1)[1].strip().lower()
    parsed = _parse_reasoning_value(requested, current_reasoning)
    if parsed is None:
        parsed = current_reasoning

    new_reasoning = db.set_reasoning_enabled(uid, parsed)
    await query.edit_message_text(
        _performance_status_text(current_mode, new_reasoning),
        reply_markup=_performance_keyboard(current_mode, new_reasoning),
        parse_mode="Markdown",
    )


async def cmd_fast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not db.has_user_profile(uid):
        context.user_data[_ONBOARDING_STEP_KEY] = "name"
        await update.message.reply_text("Please complete onboarding first. What's your name?")
        return
    fast_model = os.getenv("FAST_MODEL", "").strip()
    if not fast_model:
        await update.message.reply_text("Fast model not configured. Set FAST_MODEL in Settings.")
        return
    provider, _ = model_router.resolve_model(fast_model)
    if not model_router._provider_enabled(provider):
        cfg = model_router._PROVIDERS.get(provider, {})
        env_key = cfg.get("env_key", "API_KEY")
        await update.message.reply_text(
            f"Fast model \"{fast_model}\" requires provider \"{provider}\", but {env_key} is not set. Add your API key in Settings to use /fast."
        )
        return
    context.user_data["_fast_next"] = fast_model
    await update.message.reply_text(f"Next message will use {fast_model}.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not db.has_user_profile(uid):
        context.user_data[_ONBOARDING_STEP_KEY] = "name"
        await update.message.reply_text("Please complete onboarding first. What's your name?")
        return
    removed = db.clear_history(uid)
    await update.message.reply_text(f"Chat history cleared ({removed} messages removed).")


async def _send_message_with_retry(chat, chunk: str, max_attempts: int = 5) -> None:
    backoff = 0.5
    for attempt in range(max_attempts):
        try:
            return await chat.send_message(chunk)
        except RetryAfter as e:
            wait_seconds = float(getattr(e, "retry_after", 1)) + 0.25
            await asyncio.sleep(min(wait_seconds, 15))
        except (TimedOut, NetworkError):
            if attempt >= max_attempts - 1:
                raise
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 6)


def _build_stream_frames(text: str) -> list[str]:
    total = len(text)
    if total <= _STREAM_MIN_CHARS:
        return [text]

    frame_count = min(_STREAM_MAX_EDITS, max(4, total // 180))
    frames: list[str] = []
    boundaries = " \n\t.,!?;:"

    for i in range(1, frame_count + 1):
        target = int(total * i / frame_count)
        if target < total:
            probe = min(total, target + 20)
            while target < probe and text[target - 1] not in boundaries:
                target += 1
        if frames and target <= len(frames[-1]):
            continue
        frames.append(text[:target])

    if not frames or frames[-1] != text:
        frames.append(text)
    return frames


async def _edit_message_with_retry(message, text: str, max_attempts: int = 5) -> None:
    backoff = 0.4
    for attempt in range(max_attempts):
        try:
            await message.edit_text(text)
            return
        except BadRequest as e:
            lowered = str(e).lower()
            if "message is not modified" in lowered or "message to edit not found" in lowered:
                return
            if attempt >= max_attempts - 1:
                raise
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 6)
        except RetryAfter as e:
            wait_seconds = float(getattr(e, "retry_after", 1)) + 0.25
            await asyncio.sleep(min(wait_seconds, 15))
        except (TimedOut, NetworkError):
            if attempt >= max_attempts - 1:
                raise
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 6)


async def _stream_send(chat, text: str, locked: bool = False) -> bool:
    sanitized = _sanitize_outgoing(text)
    if not sanitized:
        return False

    async def _send_stream_chunks() -> None:
        remaining = sanitized
        max_len = 4096
        while remaining:
            chunk = remaining[:max_len]
            remaining = remaining[max_len:]
            frames = _build_stream_frames(chunk)
            if not frames:
                continue
            sent = await _send_message_with_retry(chat, frames[0])
            if sent is None:
                continue
            for frame in frames[1:]:
                await asyncio.sleep(_STREAM_EDIT_DELAY_S)
                await _edit_message_with_retry(sent, frame)

    if locked:
        await _send_stream_chunks()
        return True

    lock = _get_chat_lock(chat.id)
    async with lock:
        await _send_stream_chunks()
    return True


async def _send_voice_with_retry(chat, audio_file, max_attempts: int = 4) -> None:
    backoff = 0.6
    for attempt in range(max_attempts):
        try:
            try:
                audio_file.seek(0)
            except Exception:
                pass
            await chat.send_voice(voice=audio_file)
            return
        except RetryAfter as e:
            wait_seconds = float(getattr(e, "retry_after", 1)) + 0.25
            await asyncio.sleep(min(wait_seconds, 15))
        except (TimedOut, NetworkError):
            if attempt >= max_attempts - 1:
                raise
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 6)


async def _send_photo_with_retry(chat, photo_file, caption: str | None = None, max_attempts: int = 4) -> None:
    backoff = 0.6
    for attempt in range(max_attempts):
        try:
            try:
                photo_file.seek(0)
            except Exception:
                pass
            await chat.send_photo(photo=photo_file, caption=caption)
            return
        except RetryAfter as e:
            wait_seconds = float(getattr(e, "retry_after", 1)) + 0.25
            await asyncio.sleep(min(wait_seconds, 15))
        except (TimedOut, NetworkError):
            if attempt >= max_attempts - 1:
                raise
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 6)


async def _safe_send(chat, text: str, locked: bool = False) -> bool:
    sanitized = _sanitize_outgoing(text)
    if not sanitized:
        return False

    async def _send_chunks() -> None:
        remaining = sanitized
        max_len = 4096
        while remaining:
            chunk = remaining[:max_len]
            await _send_message_with_retry(chat, chunk)
            remaining = remaining[max_len:]

    if locked:
        await _send_chunks()
        return True

    lock = _get_chat_lock(chat.id)
    async with lock:
        await _send_chunks()
    return True


async def _run_with_user_lock(user_id: int, chat, runner_coro):
    lock = _get_user_process_lock(user_id)
    if lock.locked():
        await _safe_send(chat, "Another request is still running. I queued this one and will respond next.")

    async with lock:
        typing_task = asyncio.create_task(_typing_loop(chat))
        try:
            await runner_coro()
        finally:
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass


async def _typing_loop(chat, interval: float = 4.0):
    while True:
        try:
            await chat.send_action(ChatAction.TYPING)
        except RetryAfter as e:
            wait_seconds = float(getattr(e, "retry_after", interval)) + 0.25
            await asyncio.sleep(min(wait_seconds, 15))
            continue
        except (TimedOut, NetworkError):
            await asyncio.sleep(min(max(interval, 1.0), 8.0))
            continue
        except Exception:
            # Keep trying during this request; task cancellation handles shutdown.
            await asyncio.sleep(min(max(interval, 1.0), 8.0))
            continue
        await asyncio.sleep(interval)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""

    if await _ensure_onboarding(update, context, text):
        return

    chat = update.message.chat
    uid = update.effective_user.id
    send_func = _make_send_func(chat)

    model_override = context.user_data.pop("_fast_next", None)

    async def _runner():
        await core.process_message(uid, text, send_func, model_override=model_override)

    try:
        await _run_with_user_lock(uid, chat, _runner)
    except Exception:
        logger.exception("Error handling text message")
        await update.message.reply_text("An internal error occurred while processing your message.")


def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        try:
            import whisper
            _whisper_model = whisper.load_model("base")
            logger.info("Loaded local Whisper model (base)")
        except Exception as e:
            logger.error(f"Failed to load Whisper model: {e}")
            return None
    return _whisper_model


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    voice = update.message.voice
    if not voice:
        return

    uid = update.effective_user.id
    chat = update.message.chat

    if not db.has_user_profile(uid):
        context.user_data[_ONBOARDING_STEP_KEY] = "name"
        await update.message.reply_text("Please complete onboarding first. What's your name?")
        return

    model = _get_whisper_model()
    if model is None:
        await update.message.reply_text("Voice transcription not available. Install whisper: pip install openai-whisper")
        return

    try:
        await chat.send_action(ChatAction.TYPING)

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            file = await voice.get_file()
            await file.download_to_drive(tmp_path)
            logger.info(f"Downloaded voice message: {voice.file_id} ({voice.duration}s)")

            result = model.transcribe(tmp_path, language="en")
            text = result.get("text", "").strip()
            logger.info(f"Transcribed: {text[:100]}...")

            if not text:
                await update.message.reply_text("Couldn't transcribe the voice message.")
                return

            await update.message.reply_text(f"Transcription: {text}")

            send_func = _make_send_func(chat)

            async def _runner():
                await core.process_message(uid, text, send_func)

            await _run_with_user_lock(uid, chat, _runner)

        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    except Exception as e:
        logger.exception("Error handling voice message")
        await update.message.reply_text("An error occurred processing your voice message.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    photos = update.message.photo
    if not photos:
        return

    uid = update.effective_user.id
    chat = update.message.chat

    if not db.has_user_profile(uid):
        context.user_data[_ONBOARDING_STEP_KEY] = "name"
        await update.message.reply_text("Please complete onboarding first. What's your name?")
        return

    photo = photos[-1]
    caption = update.message.caption or "What's in this image?"

    try:
        await chat.send_action(ChatAction.TYPING)

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            file = await photo.get_file()
            await file.download_to_drive(tmp_path)
            logger.info(f"Downloaded photo: {photo.file_id}")

            with open(tmp_path, "rb") as img_file:
                image_data = img_file.read()

            import base64
            b64_data = base64.b64encode(image_data).decode("utf-8")

            send_func = _make_send_func(chat)

            async def _runner():
                await core.process_image_message(uid, caption, b64_data, send_func)

            await _run_with_user_lock(uid, chat, _runner)

        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    except Exception as e:
        logger.exception("Error handling photo message")
        await update.message.reply_text("An error occurred processing your photo.")


def register_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("speed", cmd_speed))
    app.add_handler(CommandHandler("reasoning", cmd_reasoning))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("fast", cmd_fast))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(callback_model, pattern=r"^model:"))
    app.add_handler(CallbackQueryHandler(callback_speed, pattern=r"^speed:"))
    app.add_handler(CallbackQueryHandler(callback_reasoning, pattern=r"^reasoning:"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
