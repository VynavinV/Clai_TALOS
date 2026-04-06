# Himalaya Email

Execute email actions using the local Himalaya CLI.

**IMPORTANT:** Always use the `email_execute` tool for ALL email operations. Never use raw shell commands (cat, himalaya, etc.) to interact with email. This tool handles config paths, accounts, and error formatting automatically.

This tool supports listing, reading, sending, replying, forwarding, and mailbox operations.

## email_execute

Run one Himalaya action.

**Required parameter:**
- `action` (string)

**Supported actions:**
- `list_accounts`
- `list_folders`
- `list_messages`
- `read_message`
- `thread_message`
- `send_message`
- `download_attachments`
- `extract_message`
- `export_message`
- `reply_message`
- `forward_message`
- `move_messages`
- `copy_messages`
- `delete_messages`

**Optional common parameters:**
- `account` (string): Himalaya account alias
- `folder` (string): Source folder (defaults to inbox)

**Action-specific parameters:**
- `page` (number): Page number for `list_messages`
- `message_id` (number|string): Single id for read/reply/forward/thread
- `message_ids` (array<number|string>): Multi-id actions for move/copy/delete
- `target_folder` (string): Required for move/copy actions
- `preview` (boolean): Read/thread without marking seen (default true)
- `output_path` (string): Optional path for `read_message`/`thread_message` output text. Relative paths are saved under `data/email_exports`.
- `download_dir` (string): Optional directory for `download_attachments` (and fallback destination for `extract_message`/`export_message`). Relative paths are saved under `data/email_exports`.
- `destination_path` (string): Optional destination path for `extract_message`/`export_message`.
- `full` (boolean): For `extract_message`/`export_message` — `true` exports one raw `.eml` file, `false` exports MIME parts.
- `reply_all` (boolean): For `reply_message`
- `to`, `cc`, `bcc` (array<string>): For `send_message`
- `subject` (string): For `send_message`
- `body` (string): For send/reply/forward
- `attachments` (array<string>): For `send_message` — file paths to attach (absolute, workspace-relative, or data-relative)
- `headers` (object): Optional custom headers

## Configuration

Set these optional environment values in dashboard settings:
- `HIMALAYA_BIN`: executable name or full path
- `HIMALAYA_CONFIG`: explicit config path
- `HIMALAYA_DEFAULT_ACCOUNT`: default account alias

If not set, TALOS uses the system `himalaya` binary and default Himalaya config resolution.

## Notes

- Himalaya must be installed on the host.
- Commands fail loudly with actionable error messages.
- Reply and forward use non-interactive template pipelines to avoid editor prompts.
- Attachment paths are validated before sending and protected credential/key files are blocked.
- `read_message` and `thread_message` still return stdout; `output_path` additionally saves the same content to disk.
- `download_attachments` uses Himalaya `attachment download` and returns `downloads_dir` plus parsed `downloaded_files` when available.
- `extract_message` and `export_message` use Himalaya `message export` for message-part extraction or raw `.eml` export.
