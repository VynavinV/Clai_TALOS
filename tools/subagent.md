# subagent

Use `spawn_subagent` to delegate a focused task to a bounded internal subagent.

## When to use

- Break a large request into narrower research, planning, review, or execution passes.
- Offload focused work while the main orchestrator keeps control of the overall answer.
- Multiple `spawn_subagent` calls in a single response run in PARALLEL.

## Parameters

- `role` string, optional: Short label for the subagent's job, such as `researcher`, `planner`, `reviewer`, or `executor`.
- `task` string, required: The specific work to perform.
- `context` string, optional: Extra constraints or background.

## Telegram Messaging

Each subagent has access to `send_telegram_message` and should use it sparingly:
1. Optional brief start update (one line)
2. At most one progress update only for a milestone, blocker, key decision, or long silence
3. One completion/failure summary when done

Do not stream step-by-step logs, repetitive narration, or generic still-working pings.

Subagents sign off with their role in brackets, e.g. [researcher], so the user knows who's talking.

## Output

Returns the subagent's result to the orchestrator. Subagents cannot spawn other subagents.
