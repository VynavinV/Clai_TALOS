# Clai_TALOS Design Philosophy

## Core Principle: Simplicity Over Features

Clai_TALOS is a reaction against the complexity spiral in AI assistant projects. Most alternatives have become tech demos - impressive on paper, frustrating in practice. AI assistants should be simple enough to set up, where any grandma can download, run one script, and get an intuitive interface to set up anything else. 

## The Problem With "Platform" Thinking

Projects like OpenClaw (347k stars, 25k commits) and Claude Cowork alternatives (13k+ stars) suffer from:

1. **Silent failures** - Too many abstraction layers hide where things break
2. **Configuration hell** - 20+ channels means 20+ things to misconfigure
3. **Feature creep** - Voice, canvas, device nodes, skills registries, gateway protocols
4. **Distributed complexity** - Gateway + nodes + channels = many failure points
5. **Tech demo syndrome** - Impressive demos, unreliable daily use

## Our Approach: Radical Simplicity

### Single Process, Obvious Flow

```
Telegram Message → AI.py → Model API → Tool Execution → Response
                          ↓
                    Memory/Cron/Terminal
```

No gateways. No nodes. No WebSocket RPC. No device pairing. Just code.

### One Way To Do Things

| Others | Clai_TALOS |
|--------|-----------|
| 20+ messaging channels | Telegram only |
| Gateway + nodes + plugins | Single Python process |
| Skills/Plugins/MCP registry | Tools in a folder (tools/*.md) |
| Multi-agent workforce | spawn_subagent when needed |
| 500+ SaaS integrations | terminal + web_search + scrape_url |
| Device nodes (iOS/Android) | Not our problem |
| Voice/Canvas/Visual | Not our problem |

### Fail Loudly, Not Silently

- Errors are logged and surfaced
- No silent fallbacks that hide problems
- Stack traces are good, actually
- If something breaks, you should know

### Boring Technology

- SQLite for data (not distributed databases)
- File-based tools (not plugin registries)
- HTTP polling (not WebSocket everything)
- Python stdlib + minimal deps

## What We Optimize For

1. **Reliability** - Works the same way every time
2. **Debuggability** - When it breaks, you can find why
3. **Understandability** - New developer can read AI.py and get it
4. **Modify-ability** - Want a new tool? Add a function and a .md file

## What We Explicitly Don't Do

- Multi-platform messaging (use OpenClaw if you need WhatsApp)
- Team collaboration (use Claude Cowork alternatives)
- SaaS integrations beyond web scraping

## The 850 Line Test

AI.py is ~850 lines. If a feature would double that, we don't add it. Complexity budget is real.



## Design Decisions Log

### Why Telegram Only?
One channel done well > twenty channels done poorly.

### Why Python?
Readability. Every developer can read and modify Python.

### Why File-Based Tools?
No build step, no registry, no versioning hell. Just markdown files.

### Why SQLite?
Zero config, single file, works everywhere.

### Why No Plugin System?
Plugins add complexity. If you want a new tool, write a function.

### Why OpenRouter?
One API key, 200+ models. Same OpenAI-compatible interface we already use for NVIDIA and Cerebras. No new abstraction needed — just another provider with a different base URL.

### Why Ollama?
Local models, no API key, no cloud dependency. Uses the same OpenAI-compatible API. Type a model name, it gets pulled. If Ollama isn't running, you get a clear error — no silent fallback.


## The Anti-Pattern List

Things we've seen in other projects that we avoid:

1. **Gateway architecture** - Adds latency and failure points
2. **Plugin registries** - Dependency hell
3. **Multi-channel abstraction** - Leaky abstractions
4. **Silent retries** - Hides real problems
5. **Configuration DSLs** - Just use Python/JSON
6. **Feature flags** - If it's not ready, don't ship it
7. **Microservices for everything** - Monoliths are fine
8. **Event sourcing** - YAGNI

## Contributing Philosophy

When adding features, ask:

1. Does this make the system more reliable or less?
2. Can a new developer understand this in 5 minutes?
3. Does this fail loudly or silently?
4. Is this solving a real user problem or a hypothetical one?
5. Does this add lines without adding value?

If any answer is negative, don't add it.

## The Goal

A personal AI assistant that:
- You can set up in 5 minutes
- You can debug when it breaks
- You can modify when you need to
- You can trust to work reliably
- A granny can run one thing, and be walked through simply

Everything else is noise.
