"""
Discord bot integration for Clai-TALOS.
Uses Discord Gateway (WebSocket) for receiving messages and REST API for sending.
Supports text channels and voice channels (real-time voice calls).
"""
import os
import logging
import hashlib
import asyncio
import json
import base64
import struct
import socket
import time
import tempfile
import wave
import aiohttp
import core
import db
import app_paths

logger = logging.getLogger("talos/discord")

try:
    import nacl.secret
    import nacl.utils
    NACL_AVAILABLE = True
except ImportError:
    NACL_AVAILABLE = False

# Config
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
DISCORD_APPLICATION_ID = os.getenv("DISCORD_APPLICATION_ID", "")
DISCORD_ENABLED = os.getenv("DISCORD_ENABLED", "false").lower() in ("true", "1", "yes")

API_BASE = "https://discord.com/api/v10"
GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"

# Intents: GUILD_MESSAGES (1 << 9) | MESSAGE_CONTENT (1 << 15) | DIRECT_MESSAGES (1 << 12) | GUILD_VOICE_STATES (1 << 7)
INTENTS = (1 << 9) | (1 << 15) | (1 << 12) | (1 << 7)

_gateway_task = None

# Voice state
_voice_states: dict[str, dict] = {}  # guild_id -> {"channel_id": str, "user_id": str}
_voice_connections: dict[str, dict] = {}  # channel_id -> voice connection info
_voice_tasks: dict[str, asyncio.Task] = {}  # channel_id -> audio processing task


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


async def send_proactive_voice_invite(user_id: str, guild_id: str, channel_id: str, message: str = "I need your input on something. Join me in voice?") -> bool:
    """Send a proactive voice call invite to a user via DM."""
    if not DISCORD_TOKEN:
        logger.error("Discord not configured")
        return False

    # Create a DM channel with the user
    url = f"{API_BASE}/users/@me/channels"
    headers = {
        "Authorization": f"Bot {DISCORD_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {"recipient_id": user_id}

    try:
        async with aiohttp.ClientSession() as session:
            # Create DM channel
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    logger.error(f"Failed to create DM channel: {body}")
                    return False

                dm_data = await resp.json()
                dm_channel_id = dm_data.get("id")

                if not dm_channel_id:
                    logger.error("No DM channel ID returned")
                    return False

                # Send invite message
                invite_msg = f"{message}\n\n🎤 Join voice: <#{channel_id}>"
                return await _send_discord_message(dm_channel_id, invite_msg)

    except Exception as e:
        logger.exception(f"Failed to send voice invite: {e}")
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


async def _send_discord_voice(channel_id: str, audio_path: str) -> bool:
    """Send voice message via Discord REST API as attachment."""
    if not DISCORD_TOKEN:
        logger.error("Discord not configured")
        return False

    url = f"{API_BASE}/channels/{channel_id}/messages"
    headers = {
        "Authorization": f"Bot {DISCORD_TOKEN}"
    }

    try:
        # Discord uses multipart form for file uploads
        import aiohttp
        async with aiohttp.ClientSession() as session:
            with open(audio_path, 'rb') as f:
                data = aiohttp.FormData()
                data.add_field('file', f, filename='voice.mp3', content_type='audio/mpeg')
                data.add_field('content', '')

                async with session.post(url, headers=headers, data=data) as resp:
                    if resp.status in (200, 201):
                        logger.info(f"Sent Discord voice to channel {channel_id}")
                        return True
                    else:
                        body = await resp.text()
                        logger.error(f"Discord API error {resp.status}: {body}")
                        return False
    except Exception as e:
        logger.exception(f"Failed to send Discord voice: {e}")
        return False


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
                if voice:
                    # Generate voice and send as audio attachment
                    import tempfile
                    from voice import text_to_speech, cleanup_audio_file
                    with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as f:
                        audio_path = f.name
                    try:
                        if text_to_speech(cleaned, audio_path):
                            await _send_discord_voice(channel_id, audio_path)
                        else:
                            # Fallback to text
                            await _send_discord_message(channel_id, cleaned)
                    finally:
                        cleanup_audio_file(audio_path)
                else:
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


async def _handle_voice_state_update(data: dict):
    """Handle VOICE_STATE_UPDATE event - track users joining/leaving voice channels."""
    guild_id = data.get("guild_id")
    channel_id = data.get("channel_id")
    user_id = data.get("user_id")
    session_id = data.get("session_id")

    if not guild_id or not user_id:
        return

    # Track user's voice state
    if channel_id:
        # User joined a voice channel
        _voice_states[guild_id] = {
            "channel_id": channel_id,
            "user_id": user_id,
            "session_id": session_id
        }
        logger.info(f"User {user_id} joined voice channel {channel_id} in guild {guild_id}")

        # Bot should join the same voice channel
        if is_configured() and NACL_AVAILABLE:
            await _join_voice_channel(guild_id, channel_id, session_id)
    else:
        # User left voice channel
        if guild_id in _voice_states:
            old_channel = _voice_states[guild_id]["channel_id"]
            del _voice_states[guild_id]
            logger.info(f"User {user_id} left voice channel {old_channel} in guild {guild_id}")

            # Bot should leave the voice channel
            if old_channel in _voice_connections:
                await _leave_voice_channel(old_channel)


async def _handle_voice_server_update(data: dict):
    """Handle VOICE_SERVER_UPDATE event - receive voice server info for UDP connection."""
    guild_id = data.get("guild_id")
    token = data.get("token")
    endpoint = data.get("endpoint")

    if not guild_id or not token or not endpoint:
        return

    # Find the channel this guild is in
    if guild_id not in _voice_states:
        return

    channel_id = _voice_states[guild_id]["channel_id"]
    session_id = _voice_states[guild_id]["session_id"]

    logger.info(f"Voice server update for guild {guild_id}: {endpoint}")

    # Start voice connection
    await _connect_voice_udp(channel_id, endpoint, token, session_id)


async def _join_voice_channel(guild_id: str, channel_id: str, session_id: str):
    """Request to join a voice channel via Gateway."""
    # Send Voice State Update to join channel
    payload = {
        "op": 4,
        "d": {
            "guild_id": guild_id,
            "channel_id": channel_id,
            "self_mute": False,
            "self_deaf": False
        }
    }

    # This will be sent via the main gateway WebSocket
    # We need to store it and send it from the gateway loop
    global _pending_voice_join
    _pending_voice_join = payload
    logger.info(f"Requesting to join voice channel {channel_id}")


async def _connect_voice_udp(channel_id: str, endpoint: str, token: str, session_id: str):
    """Connect to voice UDP server and start listening for audio."""
    import socket

    # Extract host and port from endpoint
    # Endpoint format: "xxx.discord.gg:443"
    if ":" not in endpoint:
        logger.error(f"Invalid voice endpoint: {endpoint}")
        return

    host, port_str = endpoint.rsplit(":", 1)
    port = int(port_str)

    try:
        # Create UDP socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)

        # Store voice connection info
        _voice_connections[channel_id] = {
            "socket": sock,
            "endpoint": (host, port),
            "token": token,
            "session_id": session_id,
            "ssrc": 0,
            "sequence": 0,
            "timestamp": 0,
            "secret_key": None,
            "encryption_mode": "xsalsa20_poly1305",
            "ip": None,
            "udp_port": None
        }

        logger.info(f"Voice UDP socket created for channel {channel_id}")

        # Start Voice Gateway connection
        asyncio.create_task(_voice_gateway_loop(channel_id, host, token, session_id))

    except Exception as e:
        logger.exception(f"Failed to create voice UDP socket: {e}")


async def _voice_gateway_loop(channel_id: str, endpoint: str, token: str, session_id: str):
    """Voice Gateway WebSocket loop - handles voice protocol."""
    # Voice Gateway URL
    ws_url = f"wss://{endpoint}/?v=4"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url) as ws:
                logger.info(f"Voice Gateway connected for channel {channel_id}")

                # Receive Ready (op 2)
                msg = await ws.receive_json()
                if msg.get("op") != 2:
                    logger.error(f"Expected Voice Ready (op 2), got op {msg.get('op')}")
                    return

                ready_data = msg["d"]
                ssrc = ready_data["ssrc"]
                ip = ready_data["ip"]
                port = ready_data["port"]
                modes = ready_data["modes"]

                logger.info(f"Voice ready: ssrc={ssrc}, ip={ip}, port={port}")

                # Update connection state
                if channel_id in _voice_connections:
                    _voice_connections[channel_id]["ssrc"] = ssrc

                # IP Discovery via UDP
                if channel_id in _voice_connections:
                    await _voice_ip_discovery(channel_id, ip, port, ssrc)

                # Send Select Protocol (op 1)
                select_protocol = {
                    "op": 1,
                    "d": {
                        "protocol": "udp",
                        "data": {
                            "address": _voice_connections[channel_id]["ip"],
                            "port": _voice_connections[channel_id]["udp_port"],
                            "mode": "xsalsa20_poly1305"
                        }
                    }
                }
                await ws.send_json(select_protocol)

                # Receive Session Description (op 4) with secret key
                msg = await ws.receive_json()
                if msg.get("op") != 4:
                    logger.error(f"Expected Session Description (op 4), got op {msg.get('op')}")
                    return

                session_desc = msg["d"]
                secret_key = bytes(session_desc["secret_key"])
                mode = session_desc["mode"]

                logger.info(f"Voice session established, mode={mode}")

                # Store secret key
                if channel_id in _voice_connections:
                    _voice_connections[channel_id]["secret_key"] = secret_key
                    _voice_connections[channel_id]["encryption_mode"] = mode

                # Start audio processing task
                if channel_id not in _voice_tasks:
                    _voice_tasks[channel_id] = asyncio.create_task(_voice_audio_loop(channel_id))

                # Start heartbeat loop
                heartbeat_interval = 5.0  # Voice gateway uses 5s heartbeat
                asyncio.create_task(_voice_heartbeat_loop(ws, heartbeat_interval))

                # Keep connection alive
                while channel_id in _voice_connections:
                    try:
                        msg = await asyncio.wait_for(ws.receive_json(), timeout=1.0)
                        op = msg.get("op")
                        if op == 6:  # Heartbeat ACK
                            pass
                        elif op == 5:  # Speaking
                            # User started/stopped speaking
                            speaking = msg["d"]["speaking"]
                            user_id = msg["d"]["user_id"]
                            logger.debug(f"Voice speaking event: user={user_id}, speaking={speaking}")
                    except asyncio.TimeoutError:
                        continue
                    except Exception as e:
                        logger.debug(f"Voice Gateway message error: {e}")
                        break

    except Exception as e:
        logger.exception(f"Voice Gateway error: {e}")
    finally:
        # Cleanup on disconnect
        if channel_id in _voice_connections:
            _voice_connections[channel_id]["socket"].close()
            del _voice_connections[channel_id]
            logger.info(f"Voice connection closed for channel {channel_id}")


async def _voice_ip_discovery(channel_id: str, ip: str, port: int, ssrc: int):
    """Perform IP discovery via UDP to find our external IP and port."""
    if channel_id not in _voice_connections:
        return

    sock = _voice_connections[channel_id]["socket"]

    try:
        # Create IP discovery packet (74 bytes)
        # Type: 0x1 (request), Length: 70, SSRC, then 64 bytes of zeros
        packet = bytearray(74)
        struct.pack_into(">H", packet, 0, 0x1)  # Type
        struct.pack_into(">H", packet, 2, 70)    # Length
        struct.pack_into(">I", packet, 4, ssrc)  # SSRC

        # Send discovery request
        await asyncio.get_event_loop().sock_sendto(sock, bytes(packet), (ip, port))

        # Receive response
        data, addr = await asyncio.get_event_loop().sock_recvfrom(sock, 74)

        if len(data) >= 74:
            # Parse response
            our_ip = data[8:72].rstrip(b'\x00').decode('utf-8')
            our_port = struct.unpack(">H", data[72:74])[0]

            logger.info(f"IP discovery: our IP={our_ip}, port={our_port}")

            # Update connection state
            _voice_connections[channel_id]["ip"] = our_ip
            _voice_connections[channel_id]["udp_port"] = our_port

    except Exception as e:
        logger.exception(f"IP discovery error: {e}")


async def _voice_heartbeat_loop(ws, interval: float):
    """Send voice gateway heartbeats."""
    try:
        while True:
            await asyncio.sleep(interval)
            heartbeat = {
                "op": 3,
                "d": time.time()
            }
            await ws.send_json(heartbeat)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.debug(f"Voice heartbeat error: {e}")


async def _leave_voice_channel(channel_id: str):
    """Leave a voice channel and cleanup."""
    if channel_id in _voice_connections:
        conn = _voice_connections[channel_id]
        conn["socket"].close()
        del _voice_connections[channel_id]
        logger.info(f"Left voice channel {channel_id}")

    if channel_id in _voice_tasks:
        _voice_tasks[channel_id].cancel()
        del _voice_tasks[channel_id]


async def _voice_audio_loop(channel_id: str):
    """Main audio loop for a voice channel - receives audio, transcribes, processes, responds."""
    if channel_id not in _voice_connections:
        return

    conn = _voice_connections[channel_id]
    sock = conn["socket"]

    # Audio buffering for VAD
    audio_buffer = []
    is_speaking = False
    silence_count = 0
    silence_threshold = 30  # frames of silence before end of utterance

    try:
        while channel_id in _voice_connections:
            try:
                # Receive audio packet (non-blocking)
                data, addr = await asyncio.get_event_loop().sock_recvfrom(sock, 4096)

                if len(data) < 12:
                    continue

                # Parse RTP header
                version = (data[0] >> 6) & 0x3
                payload_type = data[1] & 0x7F
                sequence = struct.unpack("!H", data[2:4])[0]
                timestamp = struct.unpack("!I", data[4:8])[0]
                ssrc = struct.unpack("!I", data[8:12])[0]

                # Extract encrypted audio
                encrypted_audio = data[12:]

                # Decrypt audio (if we have secret key)
                if conn["secret_key"]:
                    audio_data = _decrypt_audio(encrypted_audio, conn["secret_key"], data[:12])
                    if audio_data:
                        # Simple VAD based on amplitude
                        amplitude = _calculate_amplitude(audio_data)

                        if amplitude > 100:  # Voice detected
                            is_speaking = True
                            silence_count = 0
                            audio_buffer.append(audio_data)
                        else:
                            if is_speaking:
                                silence_count += 1
                                audio_buffer.append(audio_data)

                                if silence_count >= silence_threshold:
                                    # End of utterance - transcribe
                                    is_speaking = False
                                    await _process_voice_audio(channel_id, audio_buffer)
                                    audio_buffer = []

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Voice audio receive error: {e}")
                await asyncio.sleep(0.01)

    except Exception as e:
        logger.exception(f"Voice audio loop error: {e}")
    finally:
        if audio_buffer:
            await _process_voice_audio(channel_id, audio_buffer)


def _decrypt_audio(encrypted_data: bytes, secret_key: bytes, header: bytes) -> bytes | None:
    """Decrypt audio data using xsalsa20_poly1305."""
    if not NACL_AVAILABLE:
        return None

    try:
        # Create nonce from header (first 12 bytes) padded to 24 bytes
        nonce = bytearray(24)
        nonce[:12] = header[:12]

        # Decrypt
        box = nacl.secret.SecretBox(secret_key)
        decrypted = box.decrypt(encrypted_data, bytes(nonce))
        return decrypted
    except Exception as e:
        logger.debug(f"Audio decryption error: {e}")
        return None


def _calculate_amplitude(audio_data: bytes) -> float:
    """Calculate audio amplitude for VAD."""
    if len(audio_data) < 2:
        return 0

    # Convert PCM bytes to samples (16-bit signed)
    samples = struct.unpack(f"<{len(audio_data) // 2}h", audio_data)
    if not samples:
        return 0

    # Calculate average absolute amplitude
    return sum(abs(s) for s in samples) / len(samples)


async def _process_voice_audio(channel_id: str, audio_buffer: list[bytes]):
    """Process buffered voice audio - transcribe and respond."""
    if not audio_buffer:
        return

    # Combine audio buffer
    audio_data = b''.join(audio_buffer)

    # Skip if too short (< 1 second at 48kHz, 2 bytes per sample)
    if len(audio_data) < 96000:
        return

    # Save to temp WAV file
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
        temp_path = f.name
        with wave.open(f, 'wb') as wf:
            wf.setnchannels(1)  # mono (Discord voice is mono)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(48000)  # Discord uses 48kHz
            wf.writeframes(audio_data)

    try:
        # Import Whisper
        from bot_handlers import _get_whisper_model
        import whisper

        model = _get_whisper_model()
        if not model:
            logger.error("Whisper model not available for Discord voice")
            return

        # Transcribe
        audio = whisper.load_audio(temp_path)
        audio = whisper.pad_or_trim(audio)
        mel = whisper.log_mel_spectrogram(audio).to(model.device)
        _, probs = model.detect_language(mel)
        options = whisper.DecodingOptions(fp16=False)
        result = whisper.decode(model, mel, options)
        transcript = result.text.strip()

        if transcript and len(transcript) > 2:
            logger.info(f"Discord voice transcribed: {transcript[:50]}...")

            # Send notification
            await _send_discord_message(channel_id, f"🎤 Heard: {transcript[:100]}...")

            # Process through AI
            # Create a pseudo user_id from channel_id
            user_id = int(hashlib.sha256(f"discord_voice:{channel_id}".encode()).digest()[:8], 16)
            username = "Discord voice user"

            if not db.has_user_profile(user_id):
                db.upsert_user_profile(user_id, username, f"Discord voice user: {channel_id}")

            send_func = _make_send_func(channel_id)
            await core.process_message(user_id, transcript, send_func)

    except Exception as e:
        logger.exception(f"Discord voice transcription error: {e}")
    finally:
        try:
            os.unlink(temp_path)
        except:
            pass


# Pending voice join request
_pending_voice_join = None


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

                        # Send pending voice join request
                        global _pending_voice_join
                        if _pending_voice_join:
                            await ws.send_json(_pending_voice_join)
                            _pending_voice_join = None

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
                            elif event == "VOICE_STATE_UPDATE":
                                await _handle_voice_state_update(msg["d"])
                            elif event == "VOICE_SERVER_UPDATE":
                                await _handle_voice_server_update(msg["d"])
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
