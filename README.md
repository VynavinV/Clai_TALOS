# Clai TALOS

<p align="center">
      <img src="src/web/static/banner.png" alt="Clai TALOS logo" width="120">
</p>

Clai TALOS is a free, self-hosted AI assistant focused on practical automation through Telegram and a local web dashboard.

It is intentionally a single-process Python system: easy to run, easy to debug, and easy to modify.

If you are searching for an easier OpenClaw alternative, a free Claude Cowork alternative for personal use, or a simple AI assistant that does not require platform-level setup, Clai TALOS is built for that use case.

## Installation (Fast Start)

### Use Releases (.deb / .pkg / .app / .dmg)

For users who want the fastest install from a GitHub Release asset.

Linux/Ubuntu (.deb):

```bash
# Run in the folder where you downloaded the .deb
sudo apt install ./clai-talos_<version>_amd64.deb
sudo systemctl enable --now clai-talos
sudo systemctl status clai-talos --no-pager
```

If install fails with unmet dependencies like `python3-venv` or `python3-pip` not installable:

```bash
sudo add-apt-repository -y universe
sudo apt update
sudo apt install ./clai-talos_<version>_amd64.deb
```

macOS (.pkg):

```bash
# Run in the folder where you downloaded the .pkg
sudo installer -pkg ./clai-talos_<version>.pkg -target /

# Start and check background service
sudo launchctl kickstart -k system/com.claitalos.service
sudo launchctl print system/com.claitalos.service

# Optional: stop service
sudo launchctl bootout system /Library/LaunchDaemons/com.claitalos.service.plist

# Optional: tail logs
tail -f /usr/local/var/clai-talos/logs/stderr.log
```

macOS (.app / .dmg):

```bash
# If you downloaded the .app directly:
open "./Clai TALOS.app"

# If you downloaded the .dmg:
hdiutil attach ./clai-talos_<version>.dmg
cp -R "/Volumes/Clai TALOS <version>/Clai TALOS.app" /Applications/
hdiutil detach "/Volumes/Clai TALOS <version>"
open "/Applications/Clai TALOS.app"

# Optional: logs for the app bundle runtime
tail -f ~/.clai-talos/logs/stderr.log
```

Open the dashboard at `http://localhost:8080`.

### Manual (Clone and Run)

Download or clone this repository, then open a terminal in the project folder:

```bash
cd Clai_TALOS
```

Run one script:

Linux/macOS:

```bash
./start.sh
```

Windows:

```cmd
start.bat
```

> **Windows status:** Supported for local runtime and EXE preview builds.
> Linux/macOS remain the most-tested platforms.

Open the dashboard:

Go to `http://localhost:8080` and complete signup + onboarding.

### Use Docker

Use either local build mode (for development) or image mode (no source build).

Local build mode (from repository root):

```bash
# Build and start
docker compose up -d --build

# View logs
docker compose logs -f talos

# Stop
docker compose down
```

Image mode (pull from GitHub Container Registry) aka Docker compose:

```bash
services:
  clai-talos:
    image: ghcr.io/vynavinv/clai-talos:sha-5532fd6
    container_name: clai-talos
    restart: unless-stopped
    ports:
      - "3000:8080"
```

Open the dashboard at `http://localhost:8080`.

Notes:

- Runtime data is persisted in the Docker volume `talos-data`.
- The container sets `TALOS_DATA_DIR=/data`.
- If port `8080` is occupied, set `WEB_PORT` before launch:

```bash
WEB_PORT=8090 docker compose up -d --build
```

In image mode, use:

```bash
WEB_PORT=8090 docker compose -f docker-compose.release.yml up -d
```

If `docker: command not found` appears:

- On Windows: install Docker Desktop and enable WSL integration.
- On Linux: install Docker Engine + Compose plugin, then restart the terminal.

### Headless / SSH mode

For servers or remote machines without a browser:

```bash
./start.sh --headless
```

Windows also supports headless mode:

```cmd
start.bat --headless
```

When no config exists, headless mode offers:

1. **Tailscale + browser path** - Connect Tailscale, start Funnel, and finish onboarding from any device.
2. **Terminal-only path** - Configure Telegram, model provider, API keys, and optional services directly in terminal.

### What startup does automatically

The startup scripts (`start.sh`, `start.bat`) automatically:

1. Ensure required directories exist (`projects`, `logs/...`).
2. Verify a supported Python runtime (3.10 to 3.13).
3. Create/validate a virtual environment.
4. Install dependencies from `requirements.txt`.
5. Run `setup.py` checks (env defaults, package checks, browser defaults).
6. Launch dashboard and bot runtime.
7. On Linux, `start.sh` may configure passwordless sudo for TALOS setup tasks.

`start.sh` (Linux/macOS) also attempts best-effort Tailscale + Funnel setup.

## Sudoers Behavior (Linux)

TALOS currently uses an opinionated setup path for Linux convenience.

On Linux, `start.sh` can create `/etc/sudoers.d/clai-talos` containing a rule equivalent to:

```text
<current-user> ALL=(ALL) NOPASSWD: ALL
```

This is used so setup steps can run non-interactively.

If this is not acceptable in your environment, review `start.sh` before running TALOS.

To remove the sudoers file later:

```bash
sudo rm -f /etc/sudoers.d/clai-talos
sudo -k
```

This behavior is Linux-specific.

## Table of Contents

- [Who This Is For](#who-this-is-for)
- [Why TALOS Instead of Heavy Platforms](#why-talos-instead-of-heavy-platforms)
- [The Evolution of CLAI (2020-2026)](#the-evolution-of-clai-2020-2026)
- [Capability Overview](#capability-overview)
- [Repository Layout](#repository-layout)
- [Architecture](#architecture)
- [Sudoers Behavior (Linux)](#sudoers-behavior-linux)
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
- [Contributing](#contributing)
- [Security Reporting](#security-reporting)
- [Changelog](#changelog)
- [Documentation Map](#documentation-map)
- [License](#license)

## Who This Is For

Clai TALOS is a good fit if you want:

- a free personal AI assistant that you can self-host
- a simpler alternative to platform-style assistant stacks
- fast setup with one script and a guided onboarding flow
- local control (SQLite + files) instead of distributed infrastructure
- loud errors and straightforward debugging when something fails

It is not aimed at multi-channel enterprise orchestration, large plugin marketplaces, or microservice-heavy deployments.

## Why TALOS Instead of Heavy Platforms

The project philosophy comes from `src/docs/philosophy.md`: simplicity over features, fail loudly, and avoid architecture that hides failure points.

| Common platform pattern | Clai TALOS approach |
|-------------------------|---------------------|
| Multi-channel abstractions | Telegram first + local web dashboard |
| Gateway + nodes + plugin layers | Single Python process |
| Complex registries/config DSLs | File-based tools + straightforward config |
| Silent retries/fallback behavior | Explicit errors and visible logs |
| "Everything" feature scope | Focused personal automation |

If you need broad team collaboration or many messaging channels, OpenClaw or Claude Cowork style stacks may be a better fit. If you want a personal assistant that is easier to set up and maintain, TALOS is designed for that.

## The Evolution of CLAI (2020-2026)

CLAI began in 2020 as a personal experiment in automated interaction: a simple Discord bot built on legacy chatterbot-style architecture.

Over six years, the project evolved through repeated real-world failures, rewrites, and architectural simplification.

- 2020 (v1): Built as a reactive, Discord-based roasting bot to explore basic natural language parsing.
- 2021-2025 (v2-v3): Shifted into iterative chatbot experimentation focused on state management and response latency.
- Early 2026 (v4): Adopted early agentic framework patterns (including OpenClaw-style workflows), which exposed limitations in bloated, vision-heavy, and black-box orchestration.
- 2026 (v5 / TALOS): Rebuilt for reliability first. TALOS uses a high-density core prompt architecture, ephemeral tool injection, and a hybrid-automation design aimed at long-term maintainability.

TALOS is designed as the practical infrastructure this project needed from the beginning: user-first, inspectable, and sustainable for personal AI automation.

## Capability Overview

| Area | Capability | Main Modules |
|------|------------|--------------|
| Conversational interface | Telegram bot + web chat dashboard | `src/telegram_bot.py`, `src/bot_handlers.py`, `src/core.py` |
| Orchestration | Tool-calling agent loop, subagent delegation | `src/AI.py` |
| Persistence | SQLite settings/history/summaries | `src/db.py` |
| Memory | Keyword extraction, relevance ranking | `src/memory.py` |
| Terminal execution | Commands and workflows with safeguards | `src/terminal_tools.py` |
| Scheduling | Cron jobs using croniter | `src/cron_jobs.py` |
| Web search/scrape | Search plus local Scrapy scraping | `src/websearch.py`, `src/scrapy_scraper.py` |
| Browser automation | CDP-driven Chrome automation | `src/browser_automation.py` |
| Google bridge | OAuth + Google action execution | `src/google_integration.py` |
| Email bridge | Himalaya CLI operations | `src/email_tools.py` |
| Advanced file support | XLSX and DOCX operations | `src/spreadsheet_tools.py`, `src/docx_tools.py` |
| Live projects | Static project serving + registration | `src/gateway.py` |

## Repository Layout

TALOS keeps all source code under `src/` for a clean repository layout. Python modules, tool docs, web assets, and build scripts all live inside `src/`.

Structure overview:

- Source modules live in `src/` (`AI.py`, `core.py`, `telegram_bot.py`, `model_router.py`, etc.).
- Tool docs and usage references live in `src/tools/`.
- Dashboard pages and static assets live in `src/web/`.
- Additional documentation lives in `src/docs/`.
- Runtime-generated data stays local and ignored (`logs/`, `projects/`, `talos.db`, `.env`, `.credentials`).

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
- Dashboard signup requires at least 10 characters.
- Linux users: review [Sudoers Behavior (Linux)](#sudoers-behavior-linux), especially on shared or managed systems.
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
cd src
python3 setup.py
source venv/bin/activate
python3 telegram_bot.py
```

### Dependency management

Python dependencies are in `src/requirements.txt`.

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

### Build Windows EXE (preview)

Local build (PowerShell):

```powershell
./src/scripts/build_windows_exe.ps1
```

Each run creates a new versioned artifact using `timestamp-gitsha[-dirty]`.
You can also set your own label:

```powershell
./src/scripts/build_windows_exe.ps1 -Version "v0.2.1"
```

This generates:

- `dist/ClaiTALOS-windows-x64-<version>.zip`
- `dist/ClaiTALOS-windows-x64-latest.zip`
- `dist/SHA256SUMS.txt`
- `dist/SHA256SUMS-<version>.txt`
- `dist/build-manifest.json`

CI release build:

- Workflow: `.github/workflows/windows-exe-release.yml`
- Trigger: manual dispatch or git tags matching `v*`
- Tagged builds publish EXE zip + checksums to GitHub Releases

### Build/Publish Docker Image

Docker release image workflow:

- Workflow: `.github/workflows/docker-image-release.yml`
- Registry: `ghcr.io/vynavinv/clai-talos`
- Triggers:
      - Push to `main` (updates `latest`)
      - Tags matching `v*` (publishes version tags)
      - Manual dispatch

Use the image with:

```bash
docker compose -f src/docker-compose.release.yml up -d
```

### Build macOS PKG

On macOS:

```bash
chmod +x src/scripts/build_pkg.sh
./src/scripts/build_pkg.sh 0.1.0
```

This generates:

- `dist/pkg/clai-talos_<version>.pkg`

### Build macOS APP

On macOS:

```bash
chmod +x src/scripts/build_app.sh
./src/scripts/build_app.sh 0.1.0
```

This generates:

- `dist/app/Clai TALOS.app`

### Build macOS DMG

On macOS:

```bash
chmod +x src/scripts/build_dmg.sh
./src/scripts/build_dmg.sh 0.1.0
```

This generates:

- `dist/dmg/clai-talos_<version>.dmg`

### Extending tools

Two paths (see [MAKING_TOOLS.md](src/docs/MAKING_TOOLS.md) for full guide):

- Add native Python tool implementation and register in `src/AI.py`.
- Use dynamic tools (`create_tool`, `list_dynamic_tools`, `delete_tool`) for command-template style tools.

## Contributing

See `CONTRIBUTING.md` for local setup, coding standards, and pull request workflow.

## Security Reporting

See `SECURITY.md` for vulnerability reporting guidance.

## Changelog

See `src/docs/CHANGELOG.md` for release notes and notable changes.

## Documentation Map

Top-level docs:

- `src/docs/philosophy.md` - project philosophy and complexity boundaries
- `CONTRIBUTING.md` - contribution workflow and standards
- `SECURITY.md` - vulnerability reporting policy
- `CODE_OF_CONDUCT.md` - contributor behavior expectations
- `src/docs/CHANGELOG.md` - release history
- `src/tools/*.md` - per-tool usage documentation

Current tool docs:

- `src/docs/MAKING_TOOLS.md`
- `src/tools/browser.md`
- `src/tools/cron.md`
- `src/tools/docx_execute.md`
- `src/tools/dynamic_tools.md`
- `src/tools/email.md`
- `src/tools/file_tools.md`
- `src/tools/gateway.md`
- `src/tools/google.md`
- `src/tools/memory.md`
- `src/tools/model_prefs.md`
- `src/tools/presentation.md`
- `src/tools/scrape_url.md`
- `src/tools/spreadsheet_execute.md`
- `src/tools/subagent.md`
- `src/tools/telegram.md`
- `src/tools/terminal.md`
- `src/tools/voice.md`
- `src/tools/websearch.md`

## License

MIT (see `LICENSE`).
