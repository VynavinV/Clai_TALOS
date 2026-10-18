# Panel Popup Tool

Push a popup to the user's open Panel browser tabs (the always-on tablet screen).

**Use this freely — the Panel is a first-class surface, not a last resort.** When you
make something with a link, sketch a design, or want to show a structure visually,
put it on the Panel and mention it in one line: "I put a little design on the panel
for you, take a look." Visuals belong on the Panel; chat is for words.

## Actions

- `confirm`: blocks until the user taps Yes or No on any open Panel page,
  then returns the answer to you. Times out if nobody answers.
- `show_html`: shows a temporary custom HTML card (diagrams, tables, designs,
  links with QR codes) on all open Panel pages until dismissed or its TTL
  ends. Does NOT block — fire and forget, then continue.
- `dismiss`: close a popup you created earlier by id.

## Parameters

- `action` (required): `confirm`, `show_html`, or `dismiss`
- `title` (optional): popup heading (max 200 chars)
- `question` (confirm only): the yes/no question shown to the user
- `timeout_s` (confirm only): max seconds to wait, default 90, max 300
- `yes_label` / `no_label` (confirm only): custom button text, default Yes/No
- `html` (show_html only): self-contained HTML document (max 20000 chars,
  rendered in a sandboxed frame; scripts run but cannot touch the Panel)
- `ttl_s` (show_html only): seconds before it disappears, default 300, max 3600
- `text` (show_html only): optional plain-text note shown above the HTML
- `popup_id` (dismiss only): the id returned when the popup was created

## Writing good popup HTML

- Keep it SMALL: a few hundred lines max. One screen, one idea.
- Self-contained: inline CSS/SVG only, no external files.
- The Panel is dark-themed — use a dark background and light text, or bring
  your own background. Big fonts: it is read from across a room.
- To make a link scannable by any phone, include a QR:

```html
<img src="/api/qr?data=https%3A%2F%2Fexample.com%2Fmy-project" width="180">
```

(URL-encode the `data` parameter.)

## When to use it

- You created a project/deck/site → push a card with the link + QR.
- You want to show a diagram, layout, table, or design idea.
- A yes/no decision is blocking you → `confirm` asks on the Panel.
- NOT for every reply — popups are for things worth looking at. If no Panel
  page is open, a `confirm` just times out; then ask in chat instead.

## Examples

Show a deliverable with a QR:

```json
{"action":"show_html","title":"Your site is live","text":"Built the landing page — scan to open it.","ttl_s":900,
 "html":"<!doctype html><html><body style='margin:0;font-family:sans-serif;background:#0b1422;color:#f4f8ff;display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh'><h1>Landing page</h1><a href='https://example.com' style='color:#56ceff'>https://example.com</a><img src='/api/qr?data=https%3A%2F%2Fexample.com' width='220' alt='QR'></body></html>"}
```

Ask a yes/no question:

```json
{"action":"confirm","title":"Deploy?","question":"Ready to deploy version 1.2?","timeout_s":120}
```
