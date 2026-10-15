"""
WebRTC audio handler for real-time voice calls.
Manages WebRTC peer connections and audio streaming for browser-based voice conversations.
"""
import asyncio
import logging
import os
import tempfile
import io
import wave
import numpy as np
from typing import Optional

from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack, RTCConfiguration, RTCIceServer
from aiortc.contrib.media import MediaPlayer, MediaRelay
import av

logger = logging.getLogger("talos.webrtc")

# Global registry of active peer connections
active_connections: dict[str, RTCPeerConnection] = {}

# Registry of WebSocket connections for incoming call notifications
# Maps username -> list of WebSocket connections (supports multi-tab)
incoming_call_connections: dict[str, list[web.WebSocketResponse]] = {}

# Relay for media streams (allows multiple consumers)
relay = MediaRelay()


class AudioOutputTrack(MediaStreamTrack):
    """Audio track that sends synthesized speech back to browser."""
    kind = "audio"

    def __init__(self):
        super().__init__()
        self.audio_queue = asyncio.Queue()
        self._current_frame = None

    async def add_audio(self, pcm_data: bytes, sample_rate: int = 48000):
        """Queue PCM audio data to be sent via WebRTC."""
        # Convert PCM bytes to audio frames
        samples_per_frame = 960  # 20ms at 48kHz
        for i in range(0, len(pcm_data), samples_per_frame * 2):
            chunk = pcm_data[i:i + samples_per_frame * 2]
            if len(chunk) < samples_per_frame * 2:
                # Pad incomplete frame
                chunk = chunk + b'\x00' * (samples_per_frame * 2 - len(chunk))

            # Create audio frame
            frame = av.AudioFrame.from_ndarray(
                np.frombuffer(chunk, dtype=np.int16).reshape(1, -1),
                format='s16',
                layout='mono'
            )
            frame.sample_rate = sample_rate
            frame.pts = i // 2
            await self.audio_queue.put(frame)

    async def recv(self):
        """Return next audio frame to send to browser."""
        if self._current_frame:
            frame = self._current_frame
            self._current_frame = None
            return frame

        try:
            # Wait for audio data with timeout
            frame = await asyncio.wait_for(self.audio_queue.get(), timeout=0.02)
            return frame
        except asyncio.TimeoutError:
            # Return silence if no audio queued
            samples = np.zeros((1, 960), dtype=np.int16)
            frame = av.AudioFrame.from_ndarray(samples, format='s16', layout='mono')
            frame.sample_rate = 48000
            frame.pts = 0
            return frame


def _run_whisper(path: str) -> str:
    """Run Whisper transcription in executor (CPU-bound)."""
    from bot_handlers import _get_whisper_model
    import whisper

    model = _get_whisper_model()
    if not model:
        return ""
    audio = whisper.load_audio(path)
    audio = whisper.pad_or_trim(audio)
    mel = whisper.log_mel_spectrogram(audio).to(model.device)
    _, probs = model.detect_language(mel)
    options = whisper.DecodingOptions(fp16=False)
    result = whisper.decode(model, mel, options)
    return result.text.strip()


class AudioProcessor:
    """Processes audio from WebRTC track - buffers, detects voice, transcribes."""

    def __init__(self, user_id: int):
        self.user_id = user_id
        self.audio_buffer = []
        self.is_speaking = False
        self.silence_threshold = 30  # frames of silence before end of utterance
        self.silence_count = 0
        self.output_track = None  # Will be set when WebRTC connection established

    async def process_audio_frame(self, frame):
        """Process incoming audio frame from WebRTC track."""
        # Convert audio frame to PCM bytes
        if frame.format.name == 's16':
            pcm_data = frame.to_ndarray().tobytes()
            self.audio_buffer.append(pcm_data)

            # Simple voice activity detection based on amplitude
            audio_array = np.frombuffer(pcm_data, dtype=np.int16)
            amplitude = np.abs(audio_array).mean()

            if amplitude > 100:  # Voice detected
                self.is_speaking = True
                self.silence_count = 0
            else:
                if self.is_speaking:
                    self.silence_count += 1
                    if self.silence_count >= self.silence_threshold:
                        # End of utterance - transcribe
                        self.is_speaking = False
                        await self._transcribe_audio()

    async def _transcribe_audio(self):
        """Transcribe buffered audio using Whisper."""
        if not self.audio_buffer:
            return

        # Combine audio buffer
        audio_data = b''.join(self.audio_buffer)
        self.audio_buffer.clear()

        # Skip if too short (< 1 second at 48kHz, 2 bytes per sample)
        if len(audio_data) < 96000:
            return

        # Save to temp WAV file for Whisper
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            temp_path = f.name
            with wave.open(f, 'wb') as wf:
                wf.setnchannels(1)  # mono
                wf.setsampwidth(2)  # 16-bit
                wf.setframerate(48000)  # WebRTC uses 48kHz
                wf.writeframes(audio_data)

        try:
            # Run Whisper in executor to avoid blocking the event loop
            loop = asyncio.get_event_loop()
            transcript = await loop.run_in_executor(None, _run_whisper, temp_path)
            if transcript and len(transcript) > 2:
                logger.info(f"WebRTC transcribed: {transcript[:50]}...")
                await self._process_transcript(transcript)
        except Exception as e:
            logger.error(f"WebRTC transcription error: {e}")
        finally:
            # Cleanup temp file
            try:
                os.unlink(temp_path)
            except:
                pass

    async def _process_transcript(self, transcript: str):
        """Process transcribed text through AI and send response."""
        # Import here to avoid circular dependency
        from bot_handlers import _make_send_func

        # Create a temporary chat object for WebRTC
        class WebRTCChat:
            def __init__(self, user_id, output_track):
                self.id = user_id
                self.type = 'private'
                self.output_track = output_track

            async def send_voice(self, voice, caption=None):
                """Send voice message via WebRTC audio track."""
                if not self.output_track:
                    logger.warning(f"No output track for WebRTC user {self.id}")
                    return

                # Read the voice file and convert to PCM
                try:
                    voice_path = voice.file_path if hasattr(voice, 'file_path') else str(voice)

                    # Use ffmpeg to convert to PCM (reuse existing voice pipeline)
                    import subprocess
                    pcm_data = subprocess.run([
                        'ffmpeg', '-i', voice_path,
                        '-f', 's16le',
                        '-acodec', 'pcm_s16le',
                        '-ar', '48000',
                        '-ac', '1',
                        '-'
                    ], capture_output=True).stdout

                    # Queue audio for sending via WebRTC
                    await self.output_track.add_audio(pcm_data)
                    logger.info(f"Queued voice response for WebRTC user {self.id}")
                except Exception as e:
                    logger.error(f"Failed to send voice via WebRTC: {e}")

            async def send_message(self, text, **kwargs):
                """Send text message."""
                # For now, just log - could send via WebSocket data channel
                logger.info(f"AI response to WebRTC user {self.id}: {text[:50]}...")

        chat = WebRTCChat(self.user_id, self.output_track)
        send_func = _make_send_func(chat)

        # Process through AI
        from core import process_message
        await process_message(self.user_id, transcript, send_func)


async def handle_webrtc_signaling(request: web.Request) -> web.WebSocketResponse:
    """WebSocket endpoint for WebRTC signaling."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    username = request.get('username')
    if not username:
        await ws.close(code=4001, message=b'Authentication required')
        return ws

    logger.info(f"WebRTC signaling connection from user {username}")

    pc = RTCPeerConnection(configuration=RTCConfiguration(
        iceServers=[]  # Tailscale network - direct connection, no STUN needed
    ))
    connection_id = f"{username}_{id(pc)}"
    active_connections[connection_id] = pc

    # Deterministic hash for stable user_id across restarts
    import hashlib
    user_id = int.from_bytes(hashlib.sha256(f"webrtc:{username}".encode()).digest()[:8], 'big') % (10**9)
    audio_processor = AudioProcessor(user_id)

    # Create output track for sending audio back to browser
    output_track = AudioOutputTrack()
    audio_processor.output_track = output_track

    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        logger.info(f"WebRTC connection state: {pc.connectionState}")
        if pc.connectionState == "failed" or pc.connectionState == "closed":
            await pc.close()
            active_connections.pop(connection_id, None)

    @pc.on("track")
    def on_track(track):
        logger.info(f"WebRTC track received: {track.kind}")
        if track.kind == "audio":
            # Consume audio frames in a loop (avoids monkey-patching)
            async def consume_audio():
                while True:
                    try:
                        frame = await track.recv()
                        await audio_processor.process_audio_frame(frame)
                    except Exception:
                        break
            asyncio.create_task(consume_audio())

    # Add output track to send audio back to browser
    pc.addTrack(output_track)

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                import json
                data = json.loads(msg.data)
                msg_type = data.get('type')

                if msg_type == 'offer':
                    # Handle SDP offer from browser
                    offer = RTCSessionDescription(sdp=data['sdp'], type=data['type'])
                    await pc.setRemoteDescription(offer)

                    # Create answer
                    answer = await pc.createAnswer()
                    await pc.setLocalDescription(answer)

                    # Send answer back
                    await ws.send_json({
                        'type': pc.localDescription.type,
                        'sdp': pc.localDescription.sdp,
                    })

                elif msg_type == 'ice-candidate':
                    # Handle ICE candidate
                    candidate = data.get('candidate')
                    if candidate:
                        await pc.addIceCandidate(candidate)

                elif msg_type == 'close':
                    logger.info(f"WebRTC closing connection for user {username}")
                    await pc.close()
                    active_connections.pop(connection_id, None)
                    break

            elif msg.type == web.WSMsgType.ERROR:
                logger.error(f"WebRTC WebSocket error: {ws.exception()}")

    finally:
        # Cleanup
        await pc.close()
        active_connections.pop(connection_id, None)
        logger.info(f"WebRTC connection closed for user {username}")

    return ws


async def handle_call_page(request: web.Request) -> web.Response:
    """Serve the voice call web UI."""
    from telegram_bot import render_template
    return await render_template(request, 'call.html', {})


async def handle_incoming_call_ws(request: web.Request) -> web.WebSocketResponse:
    """WebSocket endpoint for incoming call notifications."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    username = request.get('username')
    if not username:
        await ws.close(code=4001, message=b'Authentication required')
        return ws

    logger.info(f"Incoming call WebSocket connected for user {username}")

    # Register this WebSocket for incoming call notifications
    if username not in incoming_call_connections:
        incoming_call_connections[username] = []
    incoming_call_connections[username].append(ws)

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                import json
                try:
                    data = json.loads(msg.data)
                    msg_type = data.get('type')
                    if msg_type == 'pong':
                        pass
                except:
                    pass
            elif msg.type == web.WSMsgType.ERROR:
                logger.error(f"Incoming call WebSocket error: {ws.exception()}")
    finally:
        conns = incoming_call_connections.get(username, [])
        if ws in conns:
            conns.remove(ws)
        if not conns:
            incoming_call_connections.pop(username, None)
        logger.info(f"Incoming call WebSocket disconnected for user {username}")

    return ws


async def initiate_proactive_web_call(username: str, message: str = "I need your input on something. Can we talk?") -> bool:
    """
    Initiate a proactive voice call to a web app user.
    Sends a WebSocket message to notify the user of an incoming call.
    Returns True if notification was sent successfully.
    """
    if username not in incoming_call_connections:
        logger.warning(f"User {username} not connected for incoming calls")
        return False

    conns = incoming_call_connections[username]
    sent = False
    for ws in list(conns):
        try:
            await ws.send_json({
                'type': 'incoming-call',
                'message': message
            })
            sent = True
        except Exception as e:
            logger.error(f"Failed to send incoming call notification: {e}")
            conns.remove(ws)
    if not conns:
        incoming_call_connections.pop(username, None)
    if sent:
        logger.info(f"Sent incoming call notification to {username}")
    return sent
