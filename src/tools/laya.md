# Laya Decisions (System One)

Fast typed decisions from a small model — not a chat model. Give it text plus
focused questions; get back choices, scores, and yes/no probabilities with a
confidence for each, in one call. Use it for the small judgments inside a task:
classify, route, verify, gate.

Simplicity rules:

- The answers are always one of the options you declared — nothing to parse.
- Every answer carries a confidence; below the configured threshold the caller
  should not act on it.
- If the decision layer is not configured, the tool fails loudly with the
  reason instead of guessing.

## laya_decide

Ask up to 50 typed questions about one piece of text in a single call.

**Parameters:**
- `state` (string, required): the text to judge — an email, ticket, message,
  document, or JSON-serialized record
- `questions` (object, required): map of question id -> question, where each
  question is one of:

```json
{
  "type": "choice",
  "instructions": "Which department should handle this?",
  "criteria": {
    "billing": "invoices, payments, refunds",
    "technical": "bugs, outages, system errors",
    "other": "everything else"
  }
}
```

```json
{"type": "noul", "instructions": "Does the sender request a refund?"}
```

```json
{
  "type": "score",
  "instructions": "How urgent is this?",
  "criteria": ["not urgent", "soon", "blocking"]
}
```

**Returns:**
```json
{
  "ok": true,
  "provider": "ollama",
  "latency_ms": 41.2,
  "answers": {
    "department": {"choice": "billing", "confidence": 0.98, "probabilities": {"billing": 0.98, "technical": 0.01, "other": 0.01}},
    "refund": {"noul": 0.93},
    "urgency": {"score": 2.0, "confidence": 0.81}
  }
}
```

**Reading the answers:**
- `choice` — one of your option ids, plus `probabilities` and `confidence`
- `noul` — probability the answer is yes (0.0 to 1.0)
- `score` — a position along your levels (can fall between two levels), plus `confidence`

**Guidelines:**
- Ask every question you might need in one call — they are evaluated in
  parallel and extra questions are nearly free.
- Keep each question a fast judgment ("does this message convey urgency?"),
  not an instruction ("analyze this message and decide what to do").
- Reference fields with paths when the state is JSON: "Does `order.total`
  exceed `order.budget`?"
- Do not use this tool to generate text, write code, or reason through
  multi-step plans — use your normal reasoning for that.
- A choice needs 2-255 options; always include an "other" option unless the
  list is exhaustive.

**Configuration:**
- Provider, model, and thresholds live in Settings -> Decision Model.
- Local `laya` runs through Ollama 0.40+ (free, private); a `JEV_API_KEY`
  switches to TypeSafe AI's hosted Jev model; both use the same API.
