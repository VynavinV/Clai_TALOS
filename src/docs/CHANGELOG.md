# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added

- Long-running task support: agents that use their whole round budget while still making progress now earn continuation budgets (`MAX_TASK_CONTINUATIONS`, default 6) instead of stopping at the cap.
- Context hygiene for long runs: tool results are truncated into agent context (`MAX_TOOL_RESULT_CHARS`) and older tool results inside a run are compacted past `MAX_RUN_CONTEXT_CHARS`.
- Background subagents: `spawn_subagent` accepts `background: true` for long-running detached helpers with per-subagent `timeout_s`; results are delivered back to the orchestrator between rounds, and the new `subagent_status` tool reports on them.
- Mid-run message delivery: messages sent while the assistant is working are injected into the running agent between its model/tool rounds (Telegram and web chat) instead of queueing behind the task, via the new `interrupt_hub`.
- Telegram keep-in-the-loop updates: automatic status lines ("Still working — 12 min in. Latest: ...") whenever the agent goes quiet on a long task (`TALOS_PROGRESS_*` knobs, now actually implemented and on by default), larger messaging budgets for long tasks (extra allowance per continuation, bigger subagent budgets for runs of 30+ minutes), and prompt guidance to post milestone updates.
- Watchdog escalation: hung runs are cancelled after `TALOS_STUCK_CANCEL_S` and automatically restarted once with a recovery prompt (`TALOS_STUCK_AUTO_RETRY`).
- Hard-stop auto recovery: a run with no real activity for `TALOS_STUCK_THRESHOLD_S` is now stopped completely — the run AND its background swarm — then, after a `TALOS_STUCK_RECOVERY_PAUSE_S` pause (default 5s, giving the provider time to catch up), restarted once with a recovery prompt plus an injected recovery message delivered at the run's first round boundary.
- Message interrupts stop the run: a new Telegram or web-chat message sent while the assistant is working no longer waits for a round boundary — the active run and its background swarm are stopped completely, the stop is acknowledged in chat ("Stopped the previous task… starting on your new message"), the interrupted task and any mid-run messages are persisted to history, and the new message executes as a fresh turn immediately.
- Panel confirms now ask in chat too: `panel_popup action=confirm` shows both options on the Panel AND sends the question to chat; the user can answer in either place (panel tap or a bare "yes"/"no"/label reply in Telegram or web chat), and the tool result reports where the answer came from. Confirms left pending by a stopped run are closed so they cannot swallow later replies.
- Home Assistant tool for the bot: `homeassistant_execute` (status / list_entities / get_state / call_service) with compact, filterable output, lazy-loaded behind the `smart_home` category.
- Google Calendar: `calendar.get_event`, `calendar.update_event`, and `calendar.delete_event` actions (deleting events previously always failed — the action did not exist).
- Model call resilience: `MODEL_CALL_TIMEOUT_S` default raised to 300s and timed-out calls retry the SAME model before any fallback, so long tasks no longer switch models mid-run.
- Settings UI knobs for continuations, tool result truncation, run context compaction, model call timeout, and watchdog cancel/retry behavior.
- CSRF token auto-refresh for the Panel page plus retry-on-403, and sliding renewal server-side, so an always-on panel no longer dies with "invalid or expired CSRF token" after an hour.
- Tests for the new machinery: `tests/test_long_running.py` and `tests/test_agent_continuation.py`.

### Changed

- Dashboard password minimum increased to 10 characters across shell setup and web signup.
- Signup validation now uses centralized policy in `auth_policy.py`.
- README corrected broken references and now documents repository layout and contribution/security docs.
- Core modules now resolve writable runtime files (env, db, logs, projects, oauth cache, tool configs) through centralized runtime paths for EXE-safe behavior.

### Removed

- Tracked internal planning artifact under `.kilo/`.
