# cron

Schedule recurring commands using cron syntax.

## schedule_cron

Create a cron job.

**Parameters:**
- `name` (string, required): Job name
- `schedule` (string, required): Cron schedule (e.g. `*/5 * * * *`)
- `command` (string, required): Command to run
- `timezone` (string, optional): Timezone name (default: UTC)

**Returns:**
```json
{
  "id": 1,
  "name": "disk-check",
  "schedule": "*/5 * * * *",
  "command": "df -h",
  "timezone": "UTC",
  "next_run": "2026-04-03T22:30:00Z"
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
      "name": "disk-check",
      "schedule": "*/5 * * * *",
      "command": "df -h",
      "timezone": "UTC",
      "enabled": true,
      "last_run": "2026-04-03T22:25:00Z",
      "next_run": "2026-04-03T22:30:00Z",
      "last_result": "(no output)"
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
