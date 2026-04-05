# Clai TALOS

Clai TALOS is a personal AI assistant focused on practical automation through Telegram and a local web dashboard.

It is intentionally a single-process Python system: easy to run, easy to debug, and easy to modify.

## Table of Contents

- [What This Project Is](#what-this-project-is)
- [Capability Overview](#capability-overview)
- [Architecture](#architecture)
- [Quick Start](#quick-start)
- [First Boot and Onboarding](#first-boot-and-onboarding)
- [Dashboard Guide](#dashboard-guide)
- [HTTP Routes and API Reference](#http-routes-and-api-reference)
- [Configuration Reference (.env)](#configuration-reference-env)
- [Built-in Tool Reference](#built-in-tool-reference)
- [Advanced File Support](#advanced-file-support)
- [Google Ecosystem Integration](#google-ecosystem-integration)
- [Himalaya Email Integration](#himalaya-email-integration)
- [Browser Automation Notes](#browser-automation-notes)
- [Projects Gateway](#projects-gateway)
- [Data Storage and Persistence](#data-storage-and-persistence)
- [Security Model](#security-model)
- [Operational Notes](#operational-notes)
- [Troubleshooting](#troubleshooting)
- [Development Workflow](#development-workflow)
- [Documentation Map](#documentation-map)
- [License](#license)

## What This Project Is

Clai TALOS provides a chat-first assistant that can:

- run terminal commands
- manage persistent memory
- schedule cron tasks
- search and scrape the web
- automate browser actions
- interact with Google services
- interact with email via Himalaya
- create and serve web projects
- process advanced file workflows for XLSX and DOCX

Primary design goals:

- Reliability through simple architecture
- Loud failures (visible errors instead of silent degradation)
- Fast iteration in one codebase
- Local-first operations where practical

What it is not optimized for:

- Multi-channel messenger abstraction layers
- Distributed plugin orchestration systems
- Multi-service microservice deployments

## Capability Overview

| Area | Capability | Main Modules |
|------|------------|--------------|
| Conversational interface | Telegram bot + web chat dashboard | `telegram_bot.py`, `bot_handlers.py`, `core.py` |
| Orchestration | Tool-calling agent loop, subagent delegation | `AI.py` |
| Persistence | SQLite settings/history/summaries | `db.py` |
| Memory | Keyword extraction, relevance ranking | `memory.py` |
| Terminal execution | Commands and workflows with safeguards | `terminal_tools.py` |
| Scheduling | Cron jobs using croniter | `cron_jobs.py` |
| Web search/scrape | Search plus local Scrapy scraping | `websearch.py`, `scrapy_scraper.py` |
| Browser automation | CDP-driven Chrome automation | `browser_automation.py` |
| Google bridge | OAuth + Google action execution | `google_integration.py` |
| Email bridge | Himalaya CLI operations | `email_tools.py` |
| Advanced file support | XLSX and DOCX operations | `spreadsheet_tools.py`, `docx_tools.py` |
| Live projects | Static project serving + registration | `gateway.py` |

## Architecture

### High-level flow

```text
Telegram/Web Request
        |
        v
  core.process_message
        |
        v
    AI.respond
        |
        v
  Tool Selection + Execution
        |
        +--> terminal_tools / browser_automation / websearch / ...
        |
        +--> db + memory persistence
        |
        v
Response back to Telegram or Dashboard chat
```

### Runtime component responsibilities

- `telegram_bot.py`
- Owns the aiohttp web app, auth, onboarding, dashboard APIs, and Telegram runtime lifecycle.
- `core.py`
- Receives user messages, handles simple fast paths, calls `AI.respond`, and streams progress when configured.
- `AI.py`
- Central orchestrator that builds the system prompt, exposes tool schemas, executes tool calls, and manages subagent behavior.
- `db.py`
- Manages SQLite initialization, settings, chat history, and summaries.
- `memory.py`
- Handles long-term memory storage and relevance retrieval.

### Key startup behavior

The start scripts (`start.sh`, `start.bat`) do more than launch the bot.

On startup they:

1. Ensure required directories exist (`projects`, `logs/...`).
2. Verify a supported Python runtime (3.10 to 3.13).
3. Create/validate virtual environment.
4. Install dependencies from `requirements.txt`.
5. Run `setup.py` auto-heal (env defaults, package checks, browser defaults).
6. Launch dashboard and bot runtime.

`start.sh` (Linux/macOS) also attempts best-effort Tailscale + Funnel setup.

## Quick Start

### Linux/macOS

```bash
./start.sh
```

### Windows

```cmd
start.bat
```

### Headless / SSH

For servers or remote machines without a browser, use `--headless`:

```bash
./start.sh --headless
```

Two setup modes are offered when no configuration exists:

1. **Tailscale + browser** — Ensures Tailscale is connected, starts Funnel to expose the dashboard publicly, creates a dashboard account from the terminal, and prints the URL to open on any device. The web onboarding wizard handles the rest.

2. **Terminal setup** — Walks through the full setup in the terminal: Telegram bot token, AI provider and API key (OpenAI, Anthropic, Gemini, ZhipuAI, NVIDIA, Cerebras, OpenRouter, or Ollama), model selection, optional Gemini key for web search, and optional dashboard account. Writes everything directly to `.env`.

If configuration already exists, `--headless` skips straight to starting the bot.

## First Boot and Onboarding

Unlike older versions, first run is now onboarding-first through the dashboard.

### Step 1: Create admin credentials

If no credentials exist, root route redirects to signup.

- Open `http://localhost:8080`
- Create username/password (stored in `.credentials`)
- Password is stored as bcrypt hash

### Step 2: Onboarding wizard

If `TELEGRAM_BOT_TOKEN` is missing, authenticated users are redirected to onboarding.

Onboarding endpoints support:

- Telegram token + bot name
- model provider + API key + model selection (OpenAI, Anthropic, Gemini, ZhipuAI, NVIDIA, Cerebras, OpenRouter, or Ollama)
- Gemini key
- optional Gmail/Himalaya setup
- optional Google credentials
- Tailscale status check

### Step 3: Operating mode

TALOS can run in two modes:

- Full mode: dashboard + Telegram bot connected
- Web-only mode: dashboard available, Telegram not connected yet

If Telegram startup fails, TALOS continues in web-only mode with explicit console notice.

## Dashboard Guide

Base URL: `http://localhost:8080`

Main pages:

- `/`
- Root router that redirects to signup, login, onboarding, or dashboard based on state.
- `/signup`
- First-time credential creation page.
- `/dashboard`
- Primary control panel and status view.
- `/keys`
- API key management UI.
- `/settings`
- Bot/runtime/integration configuration UI.
- `/tools`
- Toggle built-in tool permissions (`.tools_config`).
- `/projects`
- Project gateway page for generated web projects.
- `/static/chat.html`
- Native web chat UI.

## HTTP Routes and API Reference

### Auth and session routes

| Method | Route | Purpose |
|--------|-------|---------|
| GET | `/` | Root state router |
| GET | `/signup` | Signup page |
| POST | `/api/signup` | Create first credentials |
| POST | `/login` | Login |
| POST | `/logout` | Logout |

### Onboarding routes

| Method | Route | Purpose |
|--------|-------|---------|
| GET | `/onboarding` | Onboarding page |
| POST | `/api/onboarding/telegram` | Save token/name and start Telegram runtime |
| GET | `/api/onboarding/tailscale` | Check Tailscale status |
| POST | `/api/onboarding/model` | Save provider key + model preferences |
| POST | `/api/onboarding/gemini` | Save Gemini key |
| POST | `/api/onboarding/email` | Optional Gmail/Himalaya setup |
| POST | `/api/onboarding/google` | Save Google credentials |

### Dashboard API routes

| Method | Route | Purpose |
|--------|-------|---------|
| GET | `/api/status` | Runtime health summary |
| GET | `/api/keys` | Read managed key states |
| POST | `/api/keys` | Update managed keys |
| GET | `/api/settings` | Read settings values |
| POST | `/api/settings` | Persist settings values |
| GET | `/api/context-usage` | Context utilization meter |
| GET | `/api/google/status` | Google auth status |
| POST | `/api/google/connect` | Start OAuth flow |
| GET | `/oauth/google/callback` | OAuth callback |
| POST | `/api/google/disconnect` | Disconnect Google auth |
| POST | `/api/google/test` | Test Google integration |
| GET | `/api/tools` | Read enabled tool map |
| POST | `/api/tools` | Update tool map |
| GET | `/api/models` | List models |
| POST | `/api/models/fetch` | Refresh provider model list |
| POST | `/api/ollama/setup` | Install and set Ollama model |
| POST | `/api/chat` | Web chat message endpoint |
| POST | `/api/reload` | Hot reload env + clients |
| POST | `/api/restart` | Process restart |

### Static and project serving routes

| Method | Route | Purpose |
|--------|-------|---------|
| GET | `/static/*` | Dashboard static assets |
| GET | `/projects/` | Project index |
| GET | `/projects/{name}/{path:.*}` | Serve project files |
| GET | `/api/projects` | List registered projects |
| POST | `/api/projects/register` | Register a project |
| POST | `/api/projects/unregister` | Unregister a project |

## Configuration Reference (.env)

TALOS reads `.env` and supports hot reload for most runtime settings.

### Core runtime

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | empty | Telegram bot token |
| `BOT_NAME` | `Clai-TALOS` | Display name in UI |
| `WEB_PORT` | `8080` | Dashboard listen port |
| `PROJECTS_DIR` | repo `projects/` | Optional custom projects directory |

### Model providers and model selection

| Variable | Default | Description |
|----------|---------|-------------|
| `ZHIPUAI_API_KEY` | empty | Zhipu provider key |
| `GEMINI_API_KEY` | empty | Gemini provider key |
| `OPENAI_API_KEY` | empty | OpenAI provider key |
| `ANTHROPIC_API_KEY` | empty | Anthropic provider key |
| `NVIDIA_API_KEY` | empty | NVIDIA provider key |
| `CEREBRAS_API_KEY` | empty | Cerebras provider key |
| `OPENROUTER_API_KEY` | empty | OpenRouter provider key |
| `MAIN_MODEL` | auto best | Preferred text model |
| `IMAGE_MODEL` | auto best | Preferred vision/image model |
| `CLIENT_BASE_URL` | `https://api.z.ai/api/coding/paas/v4` | Zhipu API base URL |
| `NVIDIA_BASE_URL` | `https://integrate.api.nvidia.com/v1` | NVIDIA API base URL |
| `CEREBRAS_BASE_URL` | `https://api.cerebras.ai/v1` | Cerebras API base URL |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | OpenRouter API base URL |

### Ollama (local models)

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_MODEL` | empty | Ollama model name (e.g. `llama3`, `mistral`) |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Ollama API base URL |

Ollama runs models locally with no API key. Install from [ollama.com](https://ollama.com), start it, then set `OLLAMA_MODEL` to any model name. The model is pulled automatically on first use. Browse available models at [ollama.com/library](https://ollama.com/library).

### Google integration

| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_API_KEY` | empty | Optional API key |
| `GOOGLE_OAUTH_CLIENT_ID` | empty | OAuth client id |
| `GOOGLE_OAUTH_CLIENT_SECRET` | empty | OAuth client secret |
| `GOOGLE_OAUTH_REDIRECT_URI` | auto callback URL | Override OAuth callback URL |
| `GOOGLE_APPS_SCRIPT_URL` | empty | Optional Apps Script endpoint |
| `GOOGLE_OAUTH_SCOPES` | internal defaults | Optional explicit scopes |

### Himalaya email integration

| Variable | Default | Description |
|----------|---------|-------------|
| `HIMALAYA_BIN` | `himalaya` | Himalaya executable |
| `HIMALAYA_CONFIG` | empty | Config file path |
| `HIMALAYA_DEFAULT_ACCOUNT` | empty | Default account alias |

### Voice and media

| Variable | Default | Description |
|----------|---------|-------------|
| `PIPER_VOICE` | `en_US-lessac-medium` | TTS voice selector |

### Orchestrator limits and safety knobs

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_TOOL_ROUNDS` | `5` | Max function-call rounds per response |
| `MAX_TOOL_CALLS_PER_ROUND` | `20` | Cap tool calls in a single round |
| `MAX_COMMAND_TIMEOUT` | `120` | Max seconds for command tools |
| `MAX_WORKFLOW_STEPS` | `12` | Cap workflow step count |
| `MAX_ORCHESTRATOR_WALL_TIMEOUT_S` | `300` | Wall-clock budget for orchestrator run |
| `MAX_SUBAGENT_TOOL_ROUNDS` | `5` | Subagent tool rounds cap |
| `MAX_SUBAGENT_TOOL_CALLS_PER_ROUND` | `15` | Subagent calls per round cap |
| `MAX_SUBAGENT_WALL_TIMEOUT_S` | `180` | Subagent wall-clock budget |
| `SUBAGENT_MAX_TELEGRAM_MESSAGES` | `3` | Max subagent update messages |
| `SUBAGENT_MAX_TELEGRAM_MESSAGE_CHARS` | `260` | Max chars per subagent update |
| `SUBAGENT_MIN_UPDATE_INTERVAL_S` | `30` | Min spacing between subagent updates |
| `MAX_CONTEXT_CHARS` | `120000` | Context threshold shown in dashboard |

### Core progress notifier knobs

| Variable | Default | Description |
|----------|---------|-------------|
| `TALOS_PROGRESS_SILENCE_THRESHOLD_S` | `45` | Silence threshold before auto update |
| `TALOS_PROGRESS_MIN_GAP_S` | `120` | Minimum gap between auto updates |
| `TALOS_PROGRESS_CHECK_INTERVAL_S` | `10` | Progress loop tick |
| `TALOS_PROGRESS_MAX_AUTO_UPDATES` | `0` | Number of automatic progress updates (`0` disables) |

### Browser automation defaults (set by `setup.py`)

| Variable | Default | Description |
|----------|---------|-------------|
| `BROWSER_CDP_ENDPOINT` | `http://127.0.0.1:9222` | Chrome CDP endpoint |
| `BROWSER_START_IF_NEEDED` | `1` | Launch Chrome debug automatically if needed |
| `BROWSER_AUTO_CONNECT_ON_RUN` | `1` | Auto-connect before steps |
| `BROWSER_ALLOW_ISOLATED_FALLBACK` | `0` | Allow isolated fallback profile |
| `BROWSER_PROFILE_DIRECTORY` | `auto` | Use last-used Chrome profile |
| `BROWSER_STARTUP_TIMEOUT_S` | `20` | Startup timeout |
| `BROWSER_ISOLATED_PROFILE_DIR` | platform-specific | Isolated fallback profile path |
| `BROWSER_CHROME_USER_DATA_DIR` | platform-specific | Main Chrome user data path |

## Built-in Tool Reference

These tools are exposed by the orchestrator and can be toggled in the dashboard.

| Tool ID | Purpose |
|---------|---------|
| `execute_command` | Run one shell command |
| `execute_workflow` | Run multi-step command workflow |
| `schedule_cron` | Create cron schedule |
| `list_cron` | List scheduled jobs |
| `remove_cron` | Remove scheduled job |
| `save_memory` | Persist memory item |
| `search_memories` | Search memory items |
| `list_memories` | List memory items |
| `delete_memory` | Delete memory |
| `update_memory` | Update memory |
| `set_model_prefs` | Set user model preferences |
| `web_search` | Perform web search |
| `scrape_url` | Local Scrapy content extraction |
| `google_execute` | Execute Google actions |
| `email_execute` | Execute Himalaya email actions |
| `browser_start_chrome_debug` | Launch Chrome with debug port |
| `browser_connect` | Connect to browser CDP |
| `browser_run` | Execute browser action steps |
| `browser_state` | Inspect browser session state |
| `browser_disconnect` | Disconnect browser session |
| `read_file` | Read text files safely |
| `write_file` | Atomic file write/overwrite |
| `edit_file` | Exact find/replace edit |
| `spreadsheet_execute` | XLSX expert operations |
| `docx_execute` | DOCX expert operations |
| `create_tool` | Create dynamic reusable tool |
| `list_dynamic_tools` | List dynamic tools |
| `delete_tool` | Delete dynamic tool |
| `spawn_subagent` | Delegate task to subagent |
| `send_telegram_message` | Send direct Telegram text |
| `send_voice_message` | Send Telegram voice message |
| `send_telegram_photo` | Send photo to Telegram |
| `send_telegram_screenshot` | Capture and send screenshot |
| `create_project` | Create and register a live web project |
| `list_projects` | List registered projects |

## Advanced File Support

### XLSX via `spreadsheet_execute`

Supported actions:

- `read_with_pandas`
- `edit_with_openpyxl`
- `recalculate_with_libreoffice`
- `verify_formula_errors`
- `apply_financial_color_coding`

Recommended workflow for financial models:

1. Read with pandas (`read_with_pandas`) to inspect data quickly.
2. Edit with openpyxl (`edit_with_openpyxl`) so formulas and formatting are preserved.
3. Recalculate with LibreOffice (`recalculate_with_libreoffice`) using `scripts/recalc.py`.
4. Verify errors (`verify_formula_errors`) and ensure zero formula error tokens.
5. Apply financial color standards (`apply_financial_color_coding`).

Color rules implemented:

- Inputs: blue
- Formulas: black
- External links: red

LibreOffice recalculation requirements:

- LibreOffice installed
- `soffice` accessible on PATH or `LIBREOFFICE_BIN` set

### DOCX via `docx_execute`

Supported actions:

- `create_with_docx_js`
- `edit_xml`
- `track_replace`
- `set_page_size_dxa`
- `set_table_widths_dxa`
- `normalize_text`
- `validate_xml`

DOCX implementation behavior:

- New docs are generated with Node + `docx` package.
- Existing docs are modified by unpacking zip -> editing XML -> repacking.
- Tracked changes use explicit WordprocessingML tags (`w:del`, `w:ins`).
- Page and table sizing uses DXA units.
- Unicode bullets can be normalized to hyphen.
- Smart quotes/apostrophes can be normalized to XML entities.
- XML validation runs after writes.

Node requirement for DOCX creation:

```bash
npm install docx
```

## Google Ecosystem Integration

Google path is centralized via `google_execute`.

Available patterns include:

- calendar reads/creates
- drive listing/exports
- sheets read/append
- custom Apps Script forwarding if `GOOGLE_APPS_SCRIPT_URL` is set

Operational expectations:

- OAuth is required for private user data.
- `GOOGLE_API_KEY` is optional and does not replace OAuth for user-private resources.
- Integration fails loudly on auth/config errors.

## Himalaya Email Integration

Email path is centralized via `email_execute`.

Supported action families:

- account and folder listing
- message listing and reading
- thread retrieval
- send/reply/forward
- move/copy/delete

Onboarding helper can auto-configure Gmail with app password, writing config under `.himalaya/config.toml` and setting env keys.

## Browser Automation Notes

Browser automation is CDP-based and can reuse real logged-in Chrome state.

Key behavior:

- Connects to existing Chrome debug endpoint where possible
- Can start Chrome debug mode if configured
- Isolated fallback profile is opt-in (default off)
- Dashboard and tools expose state/connect/run/disconnect operations

If Chrome profile lock issues happen:

- close regular Chrome windows
- retry browser connect/start
- only enable isolated fallback if needed

## Projects Gateway

`gateway.py` serves static projects from `projects/` (or `PROJECTS_DIR`).

Main behaviors:

- Register project metadata in `projects/gateway.json`
- Serve project files at `/projects/{name}/...`
- Provide API for listing/registering projects
- Detect Tailscale base URL and produce full share links

## Data Storage and Persistence

### SQLite database

Database file: `talos.db`

Key tables initialized by `db.py`:

- `user_settings`
- `user_profiles`
- `cron_jobs`
- `chat_history`

Memory storage is initialized by `memory.py` in table `memories`.

### Important local files

| Path | Purpose |
|------|---------|
| `.env` | Runtime configuration |
| `.env.example` | Template variables |
| `.credentials` | Username + bcrypt hash |
| `.security.log` | Auth and security events |
| `.tools_config` | Enabled/disabled tool map |
| `.google_oauth.json` | OAuth token cache (if present) |
| `.himalaya/config.toml` | Email backend config (if created) |
| `logs/web_uploads/` | Uploaded dashboard files |
| `logs/browser/` | Browser automation artifacts |
| `projects/gateway.json` | Registered project map |

## Security Model

Implemented controls include:

- bcrypt password hashing in `.credentials`
- session cookies (`HttpOnly`, `SameSite=Strict`)
- CSRF token generation/validation
- login rate limiting and lockout window
- security event logging in `.security.log`
- path restrictions in file tools for protected dirs/secrets

Operational reminders:

- Use a strong unique dashboard username/password at first setup.
- Do not expose dashboard publicly without proper network controls.
- Review tool permissions in `/tools` before broad usage.

## Operational Notes

### Status checks

`/api/status` reports:

- bot runtime state
- tailscale state
- funnel state
- venv status
- credentials status
- uptime

### Hot reload vs restart

- Hot reload (`/api/reload`): reloads env and clients without process replacement.
- Restart (`/api/restart`): replaces process and restarts runtime.

### Chat context usage

`/api/context-usage` reports current context usage percentage and state (`safe`, `warning`, `critical`) for web chat profile.

## Troubleshooting

### Dashboard works but Telegram bot offline

Likely causes:

- invalid `TELEGRAM_BOT_TOKEN`
- network restrictions
- Telegram startup failure

Actions:

1. Set token in onboarding or settings.
2. Trigger hot reload.
3. Check console output for startup error details.

### Browser automation cannot connect

Likely causes:

- Chrome not running with debug endpoint
- profile lock contention

Actions:

1. Use browser start/connect tools.
2. Close normal Chrome windows and retry.
3. Enable isolated fallback only if needed.

### XLSX recalculation fails

Likely causes:

- LibreOffice missing
- `soffice` not on PATH

Actions:

1. Install LibreOffice.
2. Set `LIBREOFFICE_BIN` if needed.
3. Retry `recalculate_with_libreoffice`.

### DOCX JavaScript creation fails

Likely causes:

- Node not installed
- `docx` package missing

Actions:

1. Install Node.js.
2. Run `npm install docx`.
3. Retry `create_with_docx_js`.

### Email setup fails in onboarding

Likely causes:

- missing cargo/brew for auto-install path
- invalid Gmail app password
- himalaya binary not on PATH

Actions:

1. Install Himalaya manually.
2. Set `HIMALAYA_BIN` and `HIMALAYA_CONFIG` in settings.
3. Verify account with `email_execute` list actions.

### Ollama model not working

Likely causes:

- Ollama not installed or not running
- model name misspelled
- wrong `OLLAMA_BASE_URL`

Actions:

1. Install Ollama from [ollama.com](https://ollama.com).
2. Start Ollama (`ollama serve` or launch the app).
3. Verify with `ollama list` in terminal.
4. Check `OLLAMA_MODEL` and `OLLAMA_BASE_URL` in settings.
5. Use the "Install & Set Model" button in Settings to auto-pull.

## Development Workflow

### Local run options

Recommended:

```bash
./start.sh
```

Manual:

```bash
python3 setup.py
source venv/bin/activate
python3 telegram_bot.py
```

### Dependency management

Python dependencies are in `requirements.txt`.

Current core packages include:

- python-telegram-bot
- aiohttp
- python-dotenv
- bcrypt
- zhipuai
- google-genai
- openai
- anthropic
- httpx
- croniter
- playwright
- gTTS
- scrapy
- pandas
- openpyxl

### Extending tools

Two paths (see [tools/MAKING_TOOLS.md](tools/MAKING_TOOLS.md) for full guide):

- Add native Python tool implementation and register in `AI.py`.
- Use dynamic tools (`create_tool`, `list_dynamic_tools`, `delete_tool`) for command-template style tools.

## Documentation Map

Top-level docs:

- `CLAUDE.md` - project philosophy and complexity boundaries
- `tools/*.md` - per-tool usage documentation

Current tool docs:

- `tools/MAKING_TOOLS.md`
- `tools/browser.md`
- `tools/cron.md`
- `tools/docx_execute.md`
- `tools/dynamic_tools.md`
- `tools/email.md`
- `tools/file_tools.md`
- `tools/gateway.md`
- `tools/google.md`
- `tools/memory.md`
- `tools/model_prefs.md`
- `tools/presentation.md`
- `tools/scrape_url.md`
- `tools/spreadsheet_execute.md`
- `tools/subagent.md`
- `tools/telegram.md`
- `tools/terminal.md`
- `tools/voice.md`
- `tools/websearch.md`

## License

MIT
