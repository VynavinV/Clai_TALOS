# Browser Automation

Control a real Chrome session through CDP with deterministic actions.

This is designed for reliability:
- Reuse your existing logged-in Chrome profile
- Use structured actions (not blind coordinate clicks)
- Capture state and screenshots for debugging

## Recommended Flow

1. Usually you can call `browser_run` directly. It auto-connects and auto-starts Chrome when needed.
2. Use `browser_connect` manually if you want explicit control.
3. If endpoint is not available, `browser_connect` can start Chrome automatically.
4. Inspect with `browser_state`.
5. Clean up with `browser_disconnect`.

## browser_connect

Attach to Chrome over CDP.

**Parameters:**
- `endpoint` (string, optional): CDP endpoint (default `http://127.0.0.1:9222`)
- `start_if_needed` (boolean, optional): Launch debug Chrome automatically if endpoint is down (default true)
- `allow_isolated_fallback` (boolean, optional): Use isolated profile fallback if existing profile startup fails (default false, opt-in)
- `port` (number, optional): Debug port when auto-launching
- `chrome_path` (string, optional): Explicit Chrome executable path
- `user_data_dir` (string, optional): Chrome user data directory
- `profile_directory` (string, optional): Profile name (e.g. `Default`)
- `startup_timeout_s` (number, optional): Startup wait timeout in seconds

## browser_start_chrome_debug

Start Chrome with remote debugging enabled.

**Parameters:**
- `endpoint` (string, optional)
- `port` (number, optional)
- `chrome_path` (string, optional)
- `user_data_dir` (string, optional)
- `profile_directory` (string, optional)
- `connect` (boolean, optional): Auto-connect after start (default true)
- `startup_timeout_s` (number, optional)

## browser_run

Execute a list of steps sequentially.

**Top-level parameters:**
- `steps` (array, required)
- `stop_on_error` (boolean, optional, default true)
- `auto_connect` (boolean, optional, default true)
- `default_timeout_ms` (number, optional, default 15000)
- `page_index` (number, optional)

### Supported step actions

- `goto`: `{ action: "goto", url: "https://...", wait_until?: "domcontentloaded|load|networkidle", timeout_ms?: 20000 }`
- `click`: `{ action: "click", selector: "button.submit", button?: "left", click_count?: 1 }`
- `type`: `{ action: "type", selector: "input[name='email']", text: "you@example.com", clear?: true, delay_ms?: 0 }`
- `press`: `{ action: "press", key: "Enter", selector?: "input[name='q']" }`
- `wait_for`: `{ action: "wait_for", selector?: "#ready", text?: "Done", url_contains?: "dashboard", milliseconds?: 500 }`
- `extract`: `{ action: "extract", selector?: "body", attr?: "href", all?: false, limit?: 5 }`
- `eval`: `{ action: "eval", expression: "JSON.stringify([...document.querySelectorAll('.cw-cell')].map(c => c.textContent))" }` — evaluates a JavaScript expression in the page and returns the result as parsed JSON. Use this instead of dropping to Python scripts whenever you need structured DOM state (grid layouts, datasets, computed positions), dispatch synthetic events (KeyboardEvent/MouseEvent), or read values the `extract`/`type` vocabulary cannot express. Bare expressions (`1 + 1`, `document.title`, `JSON.stringify(...)`) need no wrapper. To pass data in, write a function and use `arg` (optional, string): `{ action: "eval", expression: "a => a.n * 2", arg: "{\"n\": 21}" }` — string `arg` values are JSON-parsed when possible, so structured payloads arrive as real objects. Results over 8KB are truncated. With `selector` set, evaluation runs against that element (like `evalOnSelector`; the element is passed as the first function argument).
- `select`: `{ action: "select", selector: "select#country", value?: "US", label?: "United States", index?: 1 }`
- `scroll`: `{ action: "scroll", selector?: "#target", x?: 0, y?: 800 }`
- `screenshot`: `{ action: "screenshot", path?: "logs/browser/my-shot.png", full_page?: false }` — if the page never settles due to perpetual CSS animations, the step automatically freezes animations and retries the capture via raw CDP (`Page.captureScreenshot`). The result reports `fallback: "animations_frozen_cdp_capture"` when this path fires. You do not need to pre-emptively disable animations.
- `assert`: `{ action: "assert", selector?: "#ok", text?: "Success", url_contains?: "done" }`
- `set_page`: `{ action: "set_page", page_index: 1 }`
- `new_tab`: `{ action: "new_tab", url?: "https://example.com" }`
- `close_tab`: `{ action: "close_tab" }`

Use `new_tab`/`close_tab` only when the user explicitly asks for multi-tab or multi-window behavior.

## browser_state

Get current connection state, URL, title, and tabs.

**Parameters:**
- `include_tabs` (boolean, optional)
- `include_page_text` (boolean, optional)
- `text_limit` (number, optional)

## browser_disconnect

Disconnect the active browser session.

**Parameters:**
- `terminate_launched` (boolean, optional): Also terminate Chrome launched by `browser_start_chrome_debug`

## Notes

- Requires Python Playwright package.
- **Where the venv lives:** the bot's Python (with Playwright installed) is at `<repo>/src/venv`. When an exploratory script needs Playwright, run it with `venv/bin/python` from `src/` — never system `python3` (Playwright is not installed there). This repo layout is the same on every deployment.
- CDP attach does not require downloading Playwright browser binaries when using your installed Chrome.
- Browser startup is on-demand when browser tools are first used.
- If profile lock is detected, close normal Chrome windows and retry the browser action.
- Isolated fallback is opt-in by default; set `BROWSER_ALLOW_ISOLATED_FALLBACK=1` only when you want fallback behavior.
- Isolated fallback profile data is stored outside the repo by default; override with `BROWSER_ISOLATED_PROFILE_DIR`.
- **Structured DOM state:** for grids, datasets, computed styles, or synthetic events, use the `eval` step — do not fall back to writing one-off Python/Playwright scripts. The `eval` action exists precisely so interactive-webapp tasks stay inside browser_run.
- **Python-side browser tool calls are audited** to `logs/audit.log` (same file as terminal commands), so post-mortems cover the full session, not just shell activity.

## Troubleshooting

- **"Playwright is not installed" from an exploratory script** — you ran system `python3` instead of the repo venv. Use `venv/bin/python`.
- **Missing Chrome/Chromium binary** — start.sh installs a system chromium or Playwright-managed Chromium as a fallback; `_default_chrome_path()` resolves in that order too. If both are missing, install one and re-run start.sh.
- **`eval` returns `undefined` for `arg`** — bare expressions cannot see `arg`. Write the function form: `expression: "a => ...", arg: "..."`.
- **Screenshot timeout on animated pages** — already self-healing: animations are frozen and the capture retried via raw CDP. No manual workaround needed.
