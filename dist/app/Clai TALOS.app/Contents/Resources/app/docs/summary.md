# TALOS Project Walkthrough Summary

Date: 2026-04-06
Project: Clai TALOS

## 1) The Big Idea (Your Manifesto, Grounded in Code)

TALOS is positioned as an Agentic Primitive, not a toolbox platform.

The philosophy is consistent across:
- README messaging (single-process, practical automation, loud failures)
- philosophy.md (simplicity over feature creep)
- system_prompt.md (do first, report second)
- actual runtime code (direct orchestration loop with tool execution)

Core claim:
- Most AI stacks are over-architected and fragile.
- TALOS compresses the stack into one direct execution engine.
- Agency comes from a clear objective + tool access + self-correction, not layers.

This is the "Hammer" thesis: fewer abstractions, more execution.

## 2) What TALOS Actually Is

At runtime, TALOS is a single Python process running:
- an aiohttp web dashboard/API server
- a Telegram bot client
- an orchestrator loop for tool-calling and subagents
- a cron scheduler loop

Primary entry flow:
- talos_entry.py -> telegram_bot.main()
- telegram_bot.main() initializes db, memory, gateway, web routes, Telegram runtime, cron loop

Main behavioral path for a user message:
1. Telegram or web chat input arrives.
2. core.process_message handles greeting/clear fast paths and watchdog setup.
3. AI.respond runs orchestration:
   - builds system context
   - selects model/provider
   - runs multi-round tool calling
   - executes tools
   - optionally spawns subagents in parallel
4. Response is persisted and returned.

## 3) Startup and Bootstrap (start.sh and setup.py)

start.sh is opinionated and high-automation:
- parses --headless
- ensures directories exist
- checks/installs Python 3.10-3.13
- installs Tailscale (best effort)
- creates/repairs venv
- installs requirements
- runs setup.py auto-heal
- optionally starts Funnel
- launches telegram_bot.py

Headless mode has two guided setup paths:
- Tailscale + browser onboarding from another device
- terminal setup wizard for Telegram/model/API keys

Important Linux behavior:
- start.sh may create /etc/sudoers.d/clai-talos with NOPASSWD: ALL for current user.
- This is convenient but high privilege and should be reviewed for shared/managed machines.

setup.py auto-heal responsibilities:
- ensures .env defaults (especially browser automation defaults)
- verifies/install missing requirements
- creates runtime directories
- migrates Himalaya config patterns

Windows parity:
- start.bat mirrors major behavior: venv setup, deps install, setup.py run, launch.
- also checks if WEB_PORT is already occupied.

## 4) Architecture by Responsibility

### Orchestration Core
- AI.py is the dominant module (largest file).
- Defines tool schema, tool dispatcher, model call loop, subagent loop, and summarization.
- Includes loop guards:
  - per-round and per-call caps
  - orchestrator and subagent wall-clock timeouts
  - blocked passive wait commands (sleep/timeout loops)

### Message Runtime
- core.py wraps orchestration with:
  - optional send wrappers (text/voice/photo)
  - stuck watchdog intervention
  - real-activity tracking
  - user-facing failure normalization

### Telegram + Web Runtime
- telegram_bot.py hosts:
  - aiohttp routes for auth, onboarding, keys, settings, chat, tools, models
  - Telegram application lifecycle (start/stop/restart)
  - security middleware and session handling
  - web chat upload handling and event streaming payloads

### Provider Layer
- model_router.py resolves provider from model string and routes calls to:
  - OpenAI
  - Anthropic
  - Gemini
  - Zhipu
  - NVIDIA
  - Cerebras
  - OpenRouter
  - Ollama
- Includes provider key gating, timeout handling, and some fallback logic.

### Persistence
- db.py: SQLite tables for user settings, profiles, chat history, cron jobs.
- memory.py: separate memory table with keyword extraction + relevance scoring.

### Automation Surfaces
- terminal_tools.py: command execution in native/docker/firejail modes with audit logging.
- browser_automation.py: CDP/Playwright automation, tab state, deterministic step engine.
- websearch.py: Gemini-based web search pipeline.
- cron_jobs.py: scheduled shell commands or self-prompts to TALOS.
- gateway.py: live project registry and static serving under /projects.

### Data and Pathing
- app_paths.py centralizes runtime paths.
- Supports source mode and frozen/bundled mode with platform-specific data roots.
- Can migrate legacy runtime data into TALOS_DATA_DIR-based structure.

## 5) Tool System Snapshot

Tool surface is broad but still centralized in one orchestrator:
- command/workflow tools
- memory tools
- web search/scrape
- google/email integrations
- browser start/connect/run/state/disconnect
- safe file read/write/edit
- spreadsheet/docx specialist tools
- dynamic tool creation/deletion
- project create/migrate/list
- subagent spawning
- Telegram output tools (text/voice/photo/screenshot/document)

Design pattern used:
- one registry of tool definitions
- one dispatcher for execution
- one loop deciding when to call tools

This keeps behavior discoverable even with many capabilities.

## 6) Security and Safety Model

Implemented controls:
- bcrypt credential hashing
- CSRF token validation
- session cookies + auth checks
- rate limiting for login attempts
- security event log file
- file path restrictions in file tools
- browser URL restrictions against private/internal address navigation
- command rate limiting and danger pattern checks in terminal executor

Notable tradeoffs:
- Linux passwordless sudo setup in start.sh is intentionally aggressive.
- Terminal execution remains inherently high-risk by design (as expected for an agentic system).

## 7) Deployment and Packaging

Supported modes:
- direct script run (start.sh/start.bat)
- Docker compose build mode
- Docker release image mode
- release artifacts (.deb, .pkg, .app, .dmg)
- Windows EXE preview/release workflow

Container posture:
- Python slim image
- non-root runtime user
- healthcheck on localhost dashboard
- TALOS_DATA_DIR volume persistence

## 8) Documentation and Developer Experience

Strong docs footprint:
- README has installation, architecture, route map, config reference, tool reference, troubleshooting, and build instructions.
- philosophy.md defines anti-bloat boundaries and contribution mindset.
- tools/*.md gives per-tool guidance.

Contribution standards and security reporting are present:
- CONTRIBUTING.md
- SECURITY.md
- CODE_OF_CONDUCT.md
- CHANGELOG.md

## 9) Current Reality vs Primitive Thesis

Where implementation strongly matches thesis:
- single-process architecture
- loud failure preference over silent magic
- direct orchestration loop
- minimal infrastructure burden
- practical local-first persistence

Where complexity is still growing (but controlled):
- AI.py and telegram_bot.py are large, central files
- many integrations increase edge-case surface
- broad toolset can drift toward toolbox behavior if not disciplined

Interpretation:
- TALOS is best described as a Primitive Core with an expanding but centralized tool surface.
- It has not fallen into distributed orchestration sprawl; the control plane is still coherent.

## 10) Practical Walkthrough Order (Fastest Way to Understand TALOS)

Read in this order:
1. README.md (what and why)
2. philosophy.md (non-negotiables)
3. start.sh and setup.py (bootstrap assumptions)
4. telegram_bot.py (runtime shell and APIs)
5. core.py (message guardrails)
6. AI.py (orchestrator brain)
7. model_router.py (provider abstraction)
8. db.py + memory.py (state model)
9. terminal_tools.py + browser_automation.py + cron_jobs.py (execution surfaces)
10. tools/*.md (usage contracts)

## 11) Bottom Line

TALOS is not trying to be a polished enterprise platform. It is a high-agency, single-process execution engine with a clear personality and intentional bias toward doing real work over architectural theater.

Your "Tool-Bloat vs Primitive" framing is not just branding. The codebase structure, startup path, and orchestration design mostly back it up.
