# Clai TALOS

A personal AI assistant for Telegram with terminal access, memory, and voice support.

## Features

- **Telegram Bot** - Chat-based interface with multi-model AI support
- **Voice Messages** - Send and receive voice messages (Whisper + gTTS)
- **Terminal Access** - Execute commands in sandboxed environment
- **Memory System** - Persistent storage with semantic search
- **Web Search** - Real-time web search and scraping
- **Cron Scheduling** - Schedule recurring tasks
- **Multi-Model** - ZhipuAI (GLM-4) and Gemini support
- **Subagent Spawning** - Parallel task delegation
- **Self-Healing** - Auto-repairs on restart
- **Web Dashboard** - Full configuration via browser

## Quick Start

**Linux/macOS:**
```bash
./start.sh
```

**Windows:**
```cmd
start.bat
```

First run will ask for:
1. Telegram bot token (required - get from @BotFather)
2. Bot name (optional - defaults to Clai-TALOS)
3. API keys (optional - can skip each or say "never ask again")

Subsequent runs are silent and auto-heal any issues.

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

## Manual Start

**Linux/macOS:**
```bash
python3 setup.py
source venv/bin/activate
python3 telegram_bot.py
```

**Windows:**
```cmd
python setup.py
venv\Scripts\activate
python telegram_bot.py
```

## Voice Support

**Incoming**: Voice messages transcribed using local Whisper (runs offline)
**Outgoing**: AI can respond with voice using gTTS

Setup:
1. Run `./start.sh` (Linux/macOS) or `start.bat` (Windows) - Whisper installs automatically
2. gTTS installs automatically with pip

See `tools/voice.md` for details.

## Architecture

```
start.sh / start.bat
    |
setup.py (auto-heal)
    |
telegram_bot.py (bot + web dashboard)
    |
AI.py (orchestrator)
    |
    +-- ZhipuAI / Gemini (models)
    +-- terminal_tools.py (command execution)
    +-- memory.py (persistent storage)
    +-- cron_jobs.py (scheduling)
    +-- websearch.py (web search)
    +-- firecrawl.py (web scraping)
    +-- voice.py (TTS/STT)
```

## Documentation

- `CLAUDE.md` - Design philosophy
- `tools/*.md` - Individual tool docs

## License

MIT
