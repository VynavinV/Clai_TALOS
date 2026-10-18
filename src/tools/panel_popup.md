# Panel Popup Tool

Push a popup to the user's open Panel browser tabs (the always-on tablet dashboard).

Use this when the user is near a Panel screen and you need a quick yes/no
decision, or when you want to show a temporary visual (diagram, table, sketch)
rendered as custom HTML.

## Capabilities

- `confirm`: blocks until the user taps Yes or No on any open Panel page,
  then returns the answer to you. Times out if nobody answers.
- `show_html`: shows a temporary custom HTML card (diagrams, charts, tables)
  on all open Panel pages until dismissed or its TTL ends. Does NOT block.
- `dismiss`: close a popup you created earlier by id.

## Parameters

- `action` (required): `confirm`, `show_html`, or `dismiss`
- `title` (optional): popup heading (max 200 chars)
- `question` (confirm only): the yes/no question shown to the user
- `timeout_s` (confirm only): max seconds to wait, default 90, max 300
- `yes_label` / `no_label` (confirm only): custom button text, default Yes/No
- `html` (show_html only): self-contained HTML fragment (max 20000 chars,
  rendered in a sandboxed frame; scripts run but cannot touch the Panel)
- `ttl_s` (show_html only): seconds before it disappears, default 300, max 3600
- `text` (show_html only): optional plain-text note shown above the HTML
- `popup_id` (dismiss only): the id returned when the popup was created

## Notes

- If no Panel page is open, a `confirm` waits its full timeout and returns
  `status: "timeout"`. Do not retry in a loop — ask in chat instead.
- Do not use this for chat conversations; the user may not be looking at the
  Panel. Use it for decisions or visuals tied to a running task.
- `show_html` HTML must be self-contained: inline CSS/SVG/JS only, no external
  files, no network assumptions.

## Examples

Ask a yes/no question:

```json
{"action":"confirm","title":"Deploy?","question":"Ready to deploy version 1.2 to the living room tablet?","timeout_s":120}
```

Show a quick diagram:

```json
{"action":"show_html","title":"Pipeline","ttl_s":600,"html":"<div style='font-family:sans-serif'><h3>Flow</h3><p>A → B → C</p></div>"}
```
