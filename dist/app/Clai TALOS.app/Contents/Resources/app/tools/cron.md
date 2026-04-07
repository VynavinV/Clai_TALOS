# cron

Schedule recurring tasks using cron syntax.

## Command modes

The `command` field supports two modes:

### Self-prompt (AI-driven)

Prefix the command with `self:` or `prompt:` to inject a message into your own AI pipeline. The text after the prefix becomes a prompt you process with full tool access — calendar, email, memory, web search, everything. The AI orchestrator handles it end-to-end and sends the result to the user's Telegram.

Use self-prompts for briefings, summaries, automated reports, reminders with context — anything that needs your brain.

Examples:
- `self:Generate my morning briefing. Check today's calendar, recent unread emails, and relevant memories. Compose a concise briefing and send it to me.`
- `prompt:Weekly review — summarize what I've been working on based on my memories and recent activity.`
- `self:It's Friday evening. Check if I have any unfinished tasks or upcoming deadlines this weekend and remind me.`

### Shell command

Any command without a `self:`/`prompt:` prefix runs as a terminal command. Output gets sent to the user as a Telegram notification.

Examples:
- `df -h`
- `echo "backup started" && ./backup.sh`

## schedule_cron

Create a cron job.

**Parameters:**
- `name` (string, required): Job name
- `schedule` (string, required): Cron schedule (e.g. `*/5 * * * *`)
- `command` (string, required): Command to run (supports `self:` prefix for AI prompts)
- `timezone` (string, optional): Timezone name (default: UTC)

**Returns:**
```json
{
  "id": 1,
  "name": "morning-briefing",
  "schedule": "30 6 * * *",
  "command": "self:Generate my morning briefing. Check today's calendar, recent unread emails, and relevant memories.",
  "timezone": "America/New_York",
  "next_run": "2026-04-06T10:30:00Z"
}
```

## list_cron

List cron jobs for the current user.

**Returns:**
```json
{
  "jobs": [
    {
      "id": 1,
      "name": "morning-briefing",
      "schedule": "30 6 * * *",
      "command": "self:Generate my morning briefing...",
      "timezone": "America/New_York",
      "enabled": true,
      "last_run": "2026-04-06T10:30:00Z",
      "next_run": "2026-04-07T10:30:00Z",
      "last_result": "Self-prompt executed: Generate my morning briefing..."
    }
  ]
}
```

## remove_cron

Delete a cron job by id.

**Parameters:**
- `job_id` (integer, required): Job id

**Returns:**
```json
{
  "ok": true
}
```
