# Laya Decision Model (System One) in Clai TALOS

TALOS integrates a "System One" decision model: a small, fast model that answers
typed questions — a choice, a score, or a yes/no probability — about any text
in a single non-autoregressive pass (~40–150 ms). It never generates text, so
there is nothing to parse and nothing to hallucinate. Every answer carries a
calibrated confidence.

The default backend is **Laya** by Convai Innovations:

- Open source, Apache 2.0 — 421M-parameter model fine-tuned from
  ModernBERT-large, trained with RLCD (calibrated decisions)
- Weights: https://huggingface.co/convaiinnovations/laya
- Repo: https://github.com/NandhaKishorM/laya — PyPI: `pip install laya`
- Served locally through **Ollama 0.40+**: `ollama pull laya` (~850 MB)
  using the Jev-compatible `/v1/systemone` endpoint

Two interchangeable backends speak the same wire protocol:

| Backend | How | Notes |
|---|---|---|
| `ollama` (default) | `ollama pull laya` | Free, local, private. Needs Ollama 0.40.0+ |
| `jev` | `JEV_API_KEY` | TypeSafe AI's hosted Jev model (https://docs.typesafe.ai) |
| `custom` | `LAYA_BASE_URL` | Any Jev-compatible `/v1/systemone` endpoint (e.g. `laya-serve`) |

Configure everything under **Settings → Decision Model** in the dashboard, or
via environment variables (below). When the decision layer is off or
unavailable, every feature below falls back to TALOS's existing behavior —
nothing breaks.

## What it does in TALOS

| Feature | What changes | Toggle |
|---|---|---|
| Email triage | `email_execute` action `triage_messages`: classifies inbox mail (urgent / needs_reply / spam / phishing / newsletter) in one fast pass and returns a digest; optional confident-spam folder moves | `LAYA_EMAIL_TRIAGE` |
| Memory relevance ranking | Picks which long-term memories matter for the current message (one call, replaces keyword-only ranking) | `LAYA_MEMORY_RERANK` |
| Context compaction | On very long tasks, decides per old tool result whether to keep it verbatim, truncate it, or drop it — important early findings survive | `LAYA_CONTEXT_COMPACT` |
| Smart model routing | Clearly simple messages go to the Quick Reply model (`FAST_MODEL`) instead of the main model | `LAYA_MODEL_TRIAGE` |
| Browser auto-pilot | `browser_auto` tool: a fast decision loop for repetitive browser flows (experimental) | `LAYA_BROWSER_AUTO` |
| `laya_decide` tool | The agent itself can ask typed questions mid-task (classify, verify, gate its own steps) | always available with the layer |

## The API in one example

```bash
curl http://localhost:11434/v1/systemone \
  -H "Content-Type: application/json" \
  -d '{
    "state": "Hi, we were billed twice for March. Please refund the duplicate today or we will cancel our plan.",
    "questions": {
      "department": {
        "type": "choice",
        "instructions": "Which department should handle this?",
        "criteria": {
          "billing": "invoices, payments, refunds",
          "technical": "bugs, outages, system errors",
          "other": "everything else"
        }
      },
      "churn_risk": {
        "type": "noul",
        "instructions": "Does the user threaten to cancel or leave?"
      }
    }
  }'
```

Response:

```json
{
  "answers": {
    "department": {"choice": "billing", "probabilities": {"billing": 0.96, "technical": 0.02, "other": 0.02}, "confidence": 0.96},
    "churn_risk": {"noul": 0.88}
  }
}
```

Question types:

- **choice** — one of your option ids (`criteria`: option id → description, 2–255 options)
- **score** — a position along ordered levels (`criteria`: list of level descriptions)
- **noul** — probability the answer is yes (0.0–1.0)

All questions in one call are evaluated in parallel against the same state.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `LAYA_ENABLED` | `1` | Master switch |
| `LAYA_PROVIDER` | `auto` | `auto` (Jev key if set, else Ollama), `ollama`, `jev`, `custom` |
| `LAYA_MODEL` | `laya` | Ollama model name (`laya`, `laya-multilingual`, `laya:latest`, ...) |
| `JEV_API_KEY` | empty | Hosted Jev backend |
| `JEV_BASE_URL` | `https://api.typesafe.ai/v1` | Hosted Jev base URL |
| `LAYA_BASE_URL` | empty | Custom Jev-compatible endpoint (e.g. `http://localhost:8000` for `laya-serve`) |
| `LAYA_TIMEOUT_S` | `5` | Per-call timeout |
| `LAYA_AUTO_PULL` | `1` | Self-healing: auto `ollama pull laya` when missing |
| `LAYA_CONFIDENCE_MIN` | `0.75` | Decisions below this confidence are ignored |
| `LAYA_EMAIL_TRIAGE` / `LAYA_MEMORY_RERANK` / `LAYA_CONTEXT_COMPACT` / `LAYA_MODEL_TRIAGE` / `LAYA_BROWSER_AUTO` | `1` | Per-feature toggles |

Dashboard: **Settings → Decision Model** — provider, model, API key, install
button (streams `ollama pull laya` progress), test button (runs one real
decision and shows the typed answers + latency), and the feature toggles.

## Operational notes

- **Ollama version check**: the `/v1/systemone` endpoint needs Ollama 0.40.0+.
  With an older Ollama the status card says so and points at the upgrade; the
  hosted Jev backend is the alternative.
- **Fail-open**: any decision-layer error (down, timeout, bad response) is
  logged once per feature per 10 minutes and the caller falls back to
  existing behavior. TALOS works exactly as before without the layer.
- **Audit trail**: every decision (feature, question types, answer values,
  latency, provider) is logged as JSON lines to `logs/laya_decisions.log`.
  State content is never logged.
- **Keep-warm**: the local model reloads in a few seconds after idle; the
  first call after a cold gap is slower, the rest are milliseconds.

## Cron idea: daily inbox digest

```
/cron
name: inbox-triage
schedule: 0 8 * * *
command: self:run email triage on my inbox and send me the digest
```

## Attribution

Laya is developed by Convai Innovations (Apache 2.0). Jev is TypeSafe AI's
hosted System One model. This file previously documented an unrelated project
that also happens to be named "Laya" (an AI notification command center) — it
was replaced with this reference for the decision model TALOS actually uses.
