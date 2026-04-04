# Clai TALOS

A personal AI assistant for Telegram with terminal access, memory, and voice support.

## Features

- **Telegram Bot** - Chat-based interface with multi-model AI support
- **Voice Messages** - Send and receive voice messages (Whisper + Piper TTS)
- **Terminal Access** - Execute commands in sandboxed environment
- **Memory System** - Persistent storage with semantic search
- **Web Search** - Real-time web search and scraping
- **Cron Scheduling** - Schedule recurring tasks
- **Multi-Model** - ZhipuAI (GLM-4) and Gemini support
- **Subagent Spawning** - Parallel task delegation
- **Self-Healing** - Auto-repairs on restart
- **Web Dashboard** - Full configuration via browser

## Quick Start

```bash
# Just run it - everything auto-configures
./start.sh
```

**First run will ask:**
1. Telegram bot token (required - get from @BotFather)
2. Bot name (optional - defaults to Clai-TALOS)
3. API keys (optional - can skip each or say "never ask again")

**Subsequent runs:**
```
./start.sh
```
- Silent startup
- Auto-heals any issues
- Never asks again

## Dashboard

Access at http://localhost:8080

- **Status**: System health monitoring
- **API Keys**: Manage all API keys
- **Tools**: Enable/disable AI tools
- **Settings**: Bot configuration, hot reload, restart

Login: `admin` / `admin` (change on first login)

## Requirements

- Python 3.10+
- Telegram Bot Token (from @BotFather)

Everything else is optional and configured via dashboard.

## Manual Start (Alternative)

If you prefer manual control:

```bash
# Run setup
python3 setup.py

# Activate venv
source venv/bin/activate

# Start bot
python3 telegram_bot.py
```

## Voice Support

**Incoming**: Voice messages transcribed using local Whisper (runs offline)  
**Outgoing**: AI can respond with voice using Piper TTS (runs locally)

Setup:
1. Just run `./start.sh` - Whisper installs automatically
2. Install Piper TTS: `brew install piper-tts` (macOS) or download binary (Linux)

See `tools/voice.md` for details.

## Self-Healing Setup

The `start.sh` script orchestrates everything:
- ✅ Runs setup.py (checks venv, packages, config)
- ✅ Starts bot automatically
- ✅ Asks for required info once (first time only)
- ✅ Remembers "never ask again" choices
- ✅ All configuration via dashboard

## Architecture

```
start.sh
    ↓
setup.py (auto-heal)
    ↓
telegram_bot.py (bot + web dashboard)
    ↓
AI.py (orchestrator)
    ↓
├── ZhipuAI / Gemini (models)
├── terminal_tools.py (command execution)
├── memory.py (persistent storage)
├── cron_jobs.py (scheduling)
├── websearch.py (web search)
├── firecrawl.py (web scraping)
└── voice.py (TTS/STT)
```

## Documentation

- `CLAUDE.md` - Design philosophy
- `toolguide.md` - Tool reference
- `tools/*.md` - Individual tool docs

## License

MIT
