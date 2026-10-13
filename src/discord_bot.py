"""
Discord bot integration for Clai-TALOS.
Uses Discord Gateway (WebSocket) for receiving messages and REST API for sending.
"""
import os
import logging
import hashlib
import asyncio
import json
import base64
import aiohttp
import core
import db
import app_paths

logger = logging.getLogger("talos/discord")

# Config
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
DISCORD_APPLICATION_ID = os.getenv("DISCORD_APPLICATION_ID", "")
DISCORD_ENABLED = os.getenv("DISCORD_ENABLED", "false").lower() in ("true", "1", "yes")

API_BASE = "https://discord.com/api/v10"
GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"

# Intents: GUILD_MESSAGES (1 << 9) | MESSAGE_CONTENT (1 << 15) | DIRECT_MESSAGES (1 << 12)
INTENTS = (1 << 9) | (1 << 15) | (1 << 12)

_gateway_task = None


def _author_to_user_id(author: dict) -> int:
    """Convert Discord user ID to stable int user_id for db."""
    discord_id = str(author.get("id", ""))
    h = hashlib.sha256(f"discord:{discord_id}".encode()).digest()
    return int.from_bytes(h[:8], 'big')


async def _send_discord_message(channel_id: str, text: str) -> bool:
    """Send text message via Discord REST API."""
    if not DISCORD_TOKEN:
        logger.error("Discord not configured")
        return False

    url = f"{API_BASE}/channels/{channel_id}/messages"
    headers = {
        "Authorization": f"Bot {DISCORD_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {"content": text}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status in (200, 201):
                    logger.info(f"Sent Discord message to channel {channel_id}")
                    return True
                else:
                    body = await resp.text()
                    logger.error(f"Discord API error {resp.status}: {body}")
                    return False
    except Exception as e:
        logger.exception(f"Failed to send Discord message: {e}")
        return False


async def _download_discord_attachment(url: str) -> bytes | None:
    """Download attachment from Discord."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.error(f"Failed to download attachment from {url}")
                    return None
                return await resp.read()
    except Exception as e:
        logger.exception(f"Failed to download Discord attachment: {e}")
        return None


def _make_send_func(channel_id: str):
    """Create send_func matching core.process_message interface."""
    async def send_func(
        msg: str = "",
        voice: bool = False,
        photo_path: str | None = None,
        caption: str = "",
        stream: bool = False,
    ) -> None:
        if msg:
            cleaned = _strip_markdown(msg)
            if cleaned:
                await _send_discord_message(channel_id, cleaned)

    return send_func


def _strip_markdown(text: str) -> str:
    """Minimal markdown stripper for Discord plain text."""
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


async def _handle_gateway_message(data: dict):
    """Handle a MESSAGE_CREATE event from the Gateway."""
    # Ignore bot's own messages
    author = data.get("author", {})
    if author.get("bot"):
        return

    channel_id = data.get("channel_id", "")
    content = data.get("content", "")

    if not channel_id:
        return

    user_id = _author_to_user_id(author)
    username = author.get("username", "Discord user")

    # Ensure user exists in db
    if not db.has_user_profile(user_id):
        db.upsert_user_profile(user_id, username, f"Discord user: {username}")
        logger.info(f"Created Discord user profile: {user_id} ({username})")

    send_func = _make_send_func(channel_id)

    # Check for attachments (images)
    attachments = data.get("attachments", [])
    image_attachment = None
    for att in attachments:
        content_type = att.get("content_type", "")
        if content_type.startswith("image/"):
            image_attachment = att
            break

    if image_attachment:
        await _send_discord_message(channel_id, "Got it. Analyzing image...")
        try:
            image_bytes = await _download_discord_attachment(image_attachment["url"])
            if not image_bytes:
                await _send_discord_message(channel_id, "Failed to download image.")
                return

            image_b64 = base64.b64encode(image_bytes).decode("utf-8")
            await core.process_image_message(user_id, content, image_b64, send_func)
        except Exception as e:
            logger.exception(f"Error processing Discord image: {e}")
            await _send_discord_message(channel_id, "An error occurred while processing the image.")
    elif content:
        try:
            await core.process_message(user_id, content, send_func)
        except Exception as e:
            logger.exception(f"Error processing Discord message: {e}")
            await _send_discord_message(channel_id, "An error occurred while processing your message.")


async def _gateway_loop():
    """Main Gateway connection loop with reconnection."""
    import aiohttp as _aiohttp

    while DISCORD_ENABLED and DISCORD_TOKEN:
        try:
            async with _aiohttp.ClientSession() as session:
                async with session.ws_connect(GATEWAY_URL) as ws:
                    logger.info("Discord Gateway connected")

                    # Receive Hello
                    msg = await ws.receive_json()
                    if msg.get("op") != 10:
                        logger.error(f"Expected Hello (op 10), got op {msg.get('op')}")
                        await asyncio.sleep(5)
                        continue

                    heartbeat_interval = msg["d"]["heartbeat_interval"] / 1000.0

                    # Send Identify
                    identify = {
                        "op": 2,
                        "d": {
                            "token": DISCORD_TOKEN,
                            "intents": INTENTS,
                            "properties": {
                                "os": "linux",
                                "browser": "talos",
                                "device": "talos"
                            }
                        }
                    }
                    await ws.send_json(identify)

                    # Heartbeat and message loop
                    last_heartbeat = 0

                    while True:
                        # Send heartbeat if needed
                        now = asyncio.get_event_loop().time()
                        if now - last_heartbeat >= heartbeat_interval:
                            await ws.send_json({"op": 1, "d": None})
                            last_heartbeat = now

                        # Wait for next message with timeout for heartbeat
                        try:
                            msg = await asyncio.wait_for(ws.receive_json(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue

                        op = msg.get("op")
                        if op == 11:  # Heartbeat ACK
                            pass
                        elif op == 0:  # Dispatch
                            event = msg.get("t", "")
                            if event == "MESSAGE_CREATE":
                                await _handle_gateway_message(msg["d"])
                        elif op == 7:  # Reconnect
                            logger.info("Discord requested reconnect")
                            break
                        elif op == 9:  # Invalid session
                            logger.warning("Discord invalid session")
                            break
                        elif op == 1:  # Heartbeat request
                            await ws.send_json({"op": 1, "d": None})
                            last_heartbeat = now

        except Exception as e:
            logger.exception(f"Discord Gateway error: {e}")

        # Reconnect after delay
        await asyncio.sleep(5)


def start_gateway():
    """Start the Discord Gateway connection as a background task."""
    global _gateway_task
    if _gateway_task is not None:
        return

    if not is_configured():
        logger.info("Discord not configured, not starting Gateway")
        return

    _gateway_task = asyncio.create_task(_gateway_loop())
    logger.info("Discord Gateway task started")


def stop_gateway():
    """Stop the Discord Gateway connection."""
    global _gateway_task
    if _gateway_task is not None:
        _gateway_task.cancel()
        _gateway_task = None
        logger.info("Discord Gateway task stopped")


def is_configured() -> bool:
    """Check if Discord is configured."""
    return bool(DISCORD_TOKEN and DISCORD_ENABLED)


def setup_routes(web_app):
    """No webhook routes needed for Discord (uses Gateway WebSocket)."""
    pass
