# Voice Tool

Send and receive voice messages on Telegram.

## Capabilities

### Incoming Voice Messages
- **Transcription**: Voice messages are automatically transcribed using local Whisper
- **Runs Offline**: No API key required, runs entirely on your machine
- **Language Support**: Optimized for English (configurable)
- **Privacy**: Audio files are deleted immediately after transcription

### Outgoing Voice Messages
- **Text-to-Speech**: AI responses can be sent as voice messages using gTTS
- **Cloud TTS**: Uses Google Text-to-Speech (requires internet)
- **Text Limit**: Up to 500 characters per voice message

## Setup

### Requirements

1. **Whisper** (for transcription) - installs automatically:
   ```bash
   pip install openai-whisper
   ```

2. **gTTS** (for voice responses) - installs automatically:
   ```bash
   pip install gTTS
   ```

No API keys required for voice features.

## Usage

### Sending Voice Messages

The AI can respond with voice using the `send_voice_message` tool:

**When to use Voice:**
- User prefers audio responses
- Longer messages (up to 500 chars)
- Accessibility needs
- Hands-free scenarios

### Receiving Voice Messages

Voice messages are handled automatically:

1. User sends voice message
2. Bot transcribes with local Whisper
3. Bot shows transcription
4. Bot processes as normal text message

## Limitations

- **TTS**: Requires internet connection (gTTS is cloud-based)
- **Text Length**: Voice responses limited to 500 characters
- **Languages**: English optimized, other languages may work but less accurately
- **First Run**: Whisper model download (~140MB) happens on first voice message

## Troubleshooting

### "Voice transcription not available"
- Install Whisper: `pip install openai-whisper`
- First voice message will download the model (~140MB)

### "Failed to generate voice message"
- Check internet connection (gTTS requires it)
- Install gTTS: `pip install gTTS`
- Check logs for errors

## Implementation Details

- **Files**: `voice.py`, `bot_handlers.py`
- **Transcription**: Local OpenAI Whisper (base model)
- **TTS**: Google Text-to-Speech (gTTS)
- **Cleanup**: Temporary audio files deleted after sending
- **Error Handling**: Falls back to text if voice fails
