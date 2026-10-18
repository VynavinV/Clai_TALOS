# subagent

Use `spawn_subagent` to delegate a task to an internal subagent.

## Parallelize by default

Subagents are the speed lever — doing everything yourself, one step at a
time, is the slow path.

- If a request has **two or more independent parts** (several files, pages,
  sources, topics, items, or questions), spawn one subagent per part **in the
  same response** — they run in PARALLEL and all results come back together.
- Split compound requests at their natural seams:
  - "find X and do Y" → researcher finds X while you set up Y
  - "compare A, B, and C" → three parallel researchers
  - "refactor these files" → one subagent per file or coherent group
  - research → write → review: delegate the research and the review
- Delegate anything focused that would otherwise occupy your rounds:
  research, data gathering, verification, testing, drafting sections. Keep
  the overall answer and the synthesis yourself.
- When in doubt between doing it yourself and delegating: delegate. An
  unnecessary subagent costs a little; serial handling of parallelizable
  work costs the user's time every round.

## Parameters

- `role` string, optional: Short label for the subagent's job, such as `researcher`, `planner`, `reviewer`, or `executor`.
- `task` string, required: The specific work to perform.
- `context` string, optional: Extra constraints or background.
- `background` boolean, optional (default false): Run detached. The tool returns
  immediately with a `subagent_id`; the subagent keeps working while you do other
  things, keeps the user posted on Telegram, and its final result is delivered to
  you automatically when it finishes. Use this for LONG tasks (roughly anything
  over 15 minutes). Check progress any time with `subagent_status`.
- `timeout_s` number, optional: Idle timeout for this subagent (max 86400).
  Long-running subagents should set e.g. 3600.

## Foreground vs background

- Foreground (default): you wait, then get the result as the tool output.
  Good for short, focused passes — and multiple foreground spawns in one
  response run in parallel.
- Background (`background: true`): you keep working. When the subagent finishes,
  its result arrives as a `[SUBAGENT RESULT]` message between your rounds —
  incorporate it into your final answer. Up to 8 background subagents at once.

## Telegram Messaging

Each subagent has access to `send_telegram_message` and should use it sparingly
on short tasks and at milestones on long ones:
1. Optional brief start update (one line)
2. One short update per real milestone — or roughly every 10 minutes on long
   tasks — so the user knows it is alive and what is happening
3. One completion/failure summary when done

Long-running subagents (idle timeout >= 30 minutes) get a larger update budget
automatically. Do not stream step-by-step logs, repetitive narration, or
generic still-working pings.

Subagents sign off with their role in brackets, e.g. [researcher], so the user knows who's talking.

## Notes

- Subagents get their own round budgets with automatic continuations while they
  make progress, so long tasks keep going instead of dying at a step cap.
- Subagents cannot spawn other subagents.
