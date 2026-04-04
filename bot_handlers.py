from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import ContextTypes, Application, MessageHandler, filters, CommandHandler, CallbackQueryHandler
import re
import logging
import os
import tempfile
import AI
import db
import zhipuai

logger = logging.getLogger("talos.handlers")

_whisper_model = None

_GREETINGS = {"hi", "hello", "hey"}
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


async def _fetch_models() -> list[str]:
    try:
        return AI.list_models()
    except Exception:
        return list(AI._MODELS.values())


def _model_keyboard(current: str, models: list[str]) -> InlineKeyboardMarkup:
    buttons = []
    for model_id in models:
        label = f"{'✓ ' if model_id == current else ''}{model_id}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"model:{model_id}")])
    return InlineKeyboardMarkup(buttons)


async def _ensure_onboarding(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> bool:
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
        await update.message.reply_text(
            "Welcome to Clai TALOS. Before we start, what's your name?"
        )
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
        await update.message.reply_text(
            "Nice to meet you. Tell me a bit about you (role, goals, or what you want help with)."
        )
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

        await update.message.reply_text(
            f"Profile saved, {name}. You're all set. Ask me anything."
        )
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


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not db.has_user_profile(uid):
        context.user_data[_ONBOARDING_STEP_KEY] = "name"
        await update.message.reply_text("Please complete onboarding first. What's your name?")
        return
    removed = db.clear_history(uid)
    await update.message.reply_text(f"Chat history cleared ({removed} messages removed).")


async def _safe_send(chat, text: str) -> None:
    """Send a message, splitting if it exceeds Telegram's 4096 char limit."""
    max_len = 4096
    while text:
        chunk = text[:max_len]
        await chat.send_message(chunk)
        text = text[max_len:]


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text if update.message.text else ""

    if await _ensure_onboarding(update, context, text):
        return

    if text.lower().strip().rstrip("!") in _GREETINGS:
        await update.message.reply_text("hello 👋 I am Clai TALOS")
        return

    chat = update.message.chat
    uid = update.effective_user.id

    async def send_func(msg: str, voice: bool = False) -> None:
        cleaned = _strip_markdown(msg)
        try:
            if voice:
                import voice as v
                audio_path = v.text_to_speech(msg)
                if audio_path:
                    await chat.send_voice(voice=open(audio_path, audio_path))
                    v.cleanup_audio_file(audio_path)
                else:
                    await _safe_send(chat, cleaned)
        except Exception:
            logger.exception("Failed to send subagent message to Telegram")

    try:
        await chat.send_action(ChatAction.TYPING)
        reply = await AI.respond(uid, text, send_func=send_func)
        cleaned = _strip_markdown(reply)
        await _safe_send(chat, cleaned)

    except zhipuai.APIReachLimitError:
        await update.message.reply_text("API rate limit or balance exhausted.")
    except zhipuai.APIStatusError as e:
        await update.message.reply_text(f"API error: {e}")
    except Exception as e:
        logger.exception("Error handling message")
        await update.message.reply_text(f"Error: {e}")


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

            await update.message.reply_text(f"🎤 _{text}_", parse_mode="Markdown")

            async def send_func(msg: str) -> None:
                cleaned = _strip_markdown(msg)
                try:
                    await _safe_send(chat, cleaned)
                except Exception:
                    logger.exception("Failed to send message")

            reply = await AI.respond(uid, text, send_func=send_func)
            cleaned = _strip_markdown(reply)
            await _safe_send(chat, cleaned)

        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    except Exception as e:
        logger.exception("Error handling voice message")
        await update.message.reply_text(f"Error: {e}")


def register_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CallbackQueryHandler(callback_model, pattern=r"^model:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
