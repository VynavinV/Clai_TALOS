# Voice Tool

Send and receive voice messages on Telegram.

## Capabilities

### Incoming Voice Messages
- **Transcription**: Voice messages are automatically transcribed using local Whisper
- **Runs Offline**: No API key required, runs entirely on your machine
- **Language Support**: Optimized for English (configurable)
- **Privacy**: Audio files are deleted immediately after transcription

### Outgoing Voice Messages
- **Text-to-Speech**: AI responses can be sent as voice messages using Piper TTS
- **Local Processing**: Runs entirely on your machine (no cloud services)
- **Voice Model**: Uses `en_US-lessac-medium` by default (configurable)

## Setup

### Requirements

1. **Whisper** (for transcription) - installs automatically:
   ```bash
   # Installed automatically by start.sh
   # Or manually: pip install openai-whisper
   ```

2. **Piper TTS** (for voice responses):
   ```bash
   # macOS
   brew install piper-tts
   
   # Linux
   wget https://github.com/rhasspy/piper/releases/download/v1.2.0/piper_arm64.tar.gz
   tar -xzf piper_arm64.tar.gz
   sudo mv piper/piper /usr/local/bin/
   
   # Or use the install script
   curl -s https://raw.githubusercontent.com/rhasspy/piper/master/src/python/install/install.sh | bash -
   ```

3. **Download Voice Model** (first run will auto-download):
   ```bash
   # Models are downloaded automatically to ~/.local/share/piper/
   # Available models:
   # - en_US-lessac-medium (default, good quality)
   # - en_US-lessac-low (faster, lower quality)
   # - en_US-amy-medium (different voice)
   ```

### Configuration

Environment variables in `.env`:

```bash
# Piper TTS (optional, for voice responses)
PIPER_VOICE=en_US-lessac-medium      # Voice model
PIPER_PATH=piper                       # Path to piper executable
PIPER_DATA_DIR=~/.local/share/piper   # Model download directory
```

Note: No API keys required! Whisper runs locally.

## Usage

### Sending Voice Messages

The AI can respond with voice using the `send_voice_message` tool:

**When to use Voice:**
- User prefers audio responses
- Longer messages (up to 500 chars)
- Accessibility needs
- Hands-free scenarios

**Example:**
```
User: *sends voice message asking about the weather*
AI: *uses send_voice_message tool*
     "The weather today is sunny with a high of 72°F..."
     *sends as voice message*
```

### Receiving Voice Messages

Voice messages are handled automatically:

1. User sends voice message
2. Bot transcribes with local Whisper
3. Bot shows transcription: `🎤 _transcribed text_`
4. Bot processes as normal text message

## Limitations

- **TTS**: Requires Piper installed locally
- **Text Length**: Voice responses limited to 500 characters
- **Languages**: English optimized, other languages may work but less accurately
- **Quality**: Depends on Piper model quality
- **First Run**: Whisper model download (~140MB) happens on first voice message

## Troubleshooting

### "Voice transcription not available"
- Install Whisper: `pip install openai-whisper`
- First voice message will download the model (~140MB)
- Check logs for model loading errors

### "Piper TTS not available"
- Install Piper: `brew install piper-tts` (mac) or download binary (Linux)
- Verify: `piper --version`
- Check logs for voice model download

### Transcription Errors
- Check available disk space (model is ~140MB)
- Verify audio file format (Telegram uses OGG/OGA)
- Check logs for Whisper errors

### TTS Errors
- Verify Piper is in PATH: `which piper`
- Check model download: `ls ~/.local/share/piper/`
- Try different voice model: `PIPER_VOICE=en_US-lessac-low`

## Implementation Details

- **File**: `voice.py`, `bot_handlers.py`
- **Transcription**: Local OpenAI Whisper (base model)
- **TTS**: Piper subprocess
- **Cleanup**: Temporary audio files deleted after sending
- **Error Handling**: Falls back to text if voice fails
