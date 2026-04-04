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

Each subagent has access to `send_telegram_message` and should use it to:
1. Introduce itself when starting (what it's about to do)
2. Send progress updates during multi-step work
3. Send its conclusion/result when done

Subagents sign off with their role in brackets, e.g. [researcher], so the user knows who's talking.

## Output

Returns the subagent's result to the orchestrator. Subagents cannot spawn other subagents.
