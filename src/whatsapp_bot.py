"""
WhatsApp Cloud API integration for Clai-TALOS.
Uses webhook routes registered in the main aiohttp web app.
"""
import os
import logging
import hashlib
import asyncio
import base64
from aiohttp import web
import aiohttp
import core
import db
import app_paths

logger = logging.getLogger("talos/whatsapp")

# Config
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
WHATSAPP_ENABLED = os.getenv("WHATSAPP_ENABLED", "false").lower() in ("true", "1", "yes")

API_VERSION = "v19.0"
BASE_URL = f"https://graph.facebook.com/{API_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}"


def _phone_to_user_id(phone: str) -> int:
    """Convert phone number to stable int user_id for db."""
    h = hashlib.sha256(f"wa:{phone}".encode()).digest()
    return int.from_bytes(h[:8], 'big')


async def _send_whatsapp_message(phone: str, text: str) -> bool:
    """Send text message via WhatsApp Cloud API."""
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        logger.error("WhatsApp not configured")
        return False

    url = f"{BASE_URL}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": text}
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status == 200:
                    logger.info(f"Sent WhatsApp message to {phone}")
                    return True
                else:
                    body = await resp.text()
                    logger.error(f"WhatsApp API error {resp.status}: {body}")
                    return False
    except Exception as e:
        logger.exception(f"Failed to send WhatsApp message: {e}")
        return False


async def _download_whatsapp_media(media_id: str) -> bytes | None:
    """Download media from WhatsApp Cloud API. Two-step: get URL, then download."""
    if not WHATSAPP_TOKEN:
        return None

    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}

    try:
        async with aiohttp.ClientSession() as session:
            # Step 1: Get media URL
            url = f"https://graph.facebook.com/{API_VERSION}/{media_id}"
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"Failed to get media URL for {media_id}: {body}")
                    return None
                data = await resp.json()
                media_url = data.get("url")
                if not media_url:
                    logger.error(f"No media URL in response for {media_id}")
                    return None

            # Step 2: Download the actual media
            async with session.get(media_url, headers=headers) as resp:
                if resp.status != 200:
                    logger.error(f"Failed to download media from {media_url}")
                    return None
                return await resp.read()

    except Exception as e:
        logger.exception(f"Failed to download WhatsApp media: {e}")
        return None


def _make_send_func(phone: str):
    """Create send_func matching core.process_message interface."""
    async def send_func(
        msg: str = "",
        voice: bool = False,
        photo_path: str | None = None,
        caption: str = "",
        stream: bool = False,
    ) -> None:
        # WhatsApp doesn't support streaming edits, just send final message
        if msg:
            # Strip markdown for WhatsApp (plain text only)
            cleaned = _strip_markdown(msg)
            if cleaned:
                await _send_whatsapp_message(phone, cleaned)

    return send_func


def _strip_markdown(text: str) -> str:
    """Minimal markdown stripper for WhatsApp plain text."""
    import re
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


async def handle_webhook_verify(request: web.Request) -> web.Response:
    """WhatsApp webhook verification (GET)."""
    params = request.query
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN and challenge:
        logger.info("WhatsApp webhook verified")
        return web.Response(text=challenge)
    else:
        logger.warning(f"WhatsApp webhook verification failed: mode={mode}")
        return web.Response(status=403, text="Forbidden")


async def handle_webhook_message(request: web.Request) -> web.Response:
    """Handle incoming WhatsApp messages (POST)."""
    if not WHATSAPP_ENABLED:
        return web.Response(status=404, text="WhatsApp not enabled")

    try:
        data = await request.json()
        logger.debug(f"WhatsApp webhook payload: {data}")

        # Extract message from webhook structure
        if "entry" not in data:
            return web.Response(status=200, text="ok")

        for entry in data["entry"]:
            for change in entry.get("changes", []):
                if change.get("field") != "messages":
                    continue

                value = change.get("value", {})
                messages = value.get("messages", [])
                contacts = value.get("contacts", [])

                if not messages:
                    continue

                # Get contact info (phone number)
                contact_map = {c["wa_id"]: c["profile"]["name"] for c in contacts}

                for msg in messages:
                    msg_type = msg.get("type", "")
                    phone = msg.get("from", "")

                    if not phone:
                        continue

                    # Map phone to user_id
                    user_id = _phone_to_user_id(phone)

                    # Ensure user exists in db (auto-create minimal profile)
                    if not db.has_user_profile(user_id):
                        name = contact_map.get(phone, phone)
                        db.upsert_user_profile(user_id, name, f"WhatsApp user: {phone}")
                        logger.info(f"Created WhatsApp user profile: {user_id} ({phone})")

                    send_func = _make_send_func(phone)

                    if msg_type == "text":
                        text = msg.get("text", {}).get("body", "")
                        if not text:
                            continue
                        try:
                            await core.process_message(user_id, text, send_func)
                        except Exception as e:
                            logger.exception(f"Error processing WhatsApp message: {e}")
                            await _send_whatsapp_message(phone, "An error occurred while processing your message.")

                    elif msg_type == "image":
                        image_data = msg.get("image", {})
                        media_id = image_data.get("id", "")
                        caption = image_data.get("caption", "")

                        if not media_id:
                            logger.warning("Image message missing media ID")
                            continue

                        await _send_whatsapp_message(phone, "Got it. Analyzing image...")

                        try:
                            image_bytes = await _download_whatsapp_media(media_id)
                            if not image_bytes:
                                await _send_whatsapp_message(phone, "Failed to download image.")
                                continue

                            image_b64 = base64.b64encode(image_bytes).decode("utf-8")
                            await core.process_image_message(user_id, caption, image_b64, send_func)
                        except Exception as e:
                            logger.exception(f"Error processing WhatsApp image: {e}")
                            await _send_whatsapp_message(phone, "An error occurred while processing the image.")

                    else:
                        logger.info(f"Skipping unsupported message type: {msg_type}")

        return web.Response(status=200, text="ok")

    except Exception as e:
        logger.exception(f"Error handling WhatsApp webhook: {e}")
        return web.Response(status=500, text="Internal error")


def is_configured() -> bool:
    """Check if WhatsApp is configured."""
    return bool(WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_VERIFY_TOKEN and WHATSAPP_ENABLED)


def setup_routes(web_app: web.Application):
    """Register WhatsApp webhook routes."""
    if not is_configured():
        logger.info("WhatsApp not configured, skipping routes")
        return

    web_app.router.add_get("/whatsapp/webhook", handle_webhook_verify)
    web_app.router.add_post("/whatsapp/webhook", handle_webhook_message)
    logger.info("WhatsApp webhook routes registered")
