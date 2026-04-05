# Google Integration

Direct Google API access using OAuth. No Apps Script required.

## Setup

OAuth is linked once from the dashboard or during onboarding. Requires:
- `GOOGLE_OAUTH_CLIENT_ID` (required)
- `GOOGLE_OAUTH_CLIENT_SECRET` (optional but recommended)

## google_execute

Run one Google action. Works directly via Google APIs.

Parameters:
- `action` (string, required): action name
- `payload` (object, optional): action-specific data

### Supported Actions

**Calendar:**
- `calendar.list_events` — List upcoming events. Payload: `{time_min, time_max, max_results, calendar_id}`
- `calendar.create_event` — Create an event. Payload: `{calendar_id, event: {summary, start: {dateTime}, end: {dateTime}, ...}}`
- `calendar.list_calendars` — List all calendars.

**Drive:**
- `drive.list_files` — List files. Payload: `{max_results, query}`
- `drive.get_file` — Get file metadata. Payload: `{file_id}`
- `drive.export_file` — Export file content. Payload: `{file_id, mime_type}`

**Sheets:**
- `sheets.get_values` — Read cells. Payload: `{spreadsheet_id, range}`
- `sheets.append_row` — Append a row. Payload: `{spreadsheet_id, range, values}`

### Custom Actions

If `GOOGLE_APPS_SCRIPT_URL` is configured, any unrecognized action is forwarded to the Apps Script bridge.

## Examples

```json
{"action": "calendar.list_events", "payload": {"max_results": 5}}
```

```json
{"action": "drive.list_files", "payload": {"max_results": 10}}
```

```json
{"action": "sheets.get_values", "payload": {"spreadsheet_id": "...", "range": "Sheet1!A1:D10"}}
```

## Notes

- `GOOGLE_API_KEY` alone cannot access private data. OAuth consent is required.
- Token auto-refreshes. If refresh fails, reconnect from dashboard.
