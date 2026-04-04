# File Editing

Read, write, and edit files with precision. Prefer `edit_file` over `write_file` for changes to existing files.

## read_file

Read file contents with line numbers. Always read a file BEFORE editing it.

**Parameters:**
- `path` (string, required): File path (absolute or relative to project root)
- `offset` (number, optional): Start line (1-indexed, default 0 = start of file)
- `limit` (number, optional): Max lines to return (default 500, max 2000)

**Returns:**
```json
{
  "path": "/absolute/path/to/file",
  "total_lines": 850,
  "offset": 0,
  "lines_shown": 500,
  "truncated": true,
  "encoding": "utf-8",
  "bytes": 32400,
  "lines": [
    {"line": 1, "content": "import os"},
    {"line": 2, "content": "import json"}
  ]
}
```

**On error, returns:**
```json
{
  "error": "File not found: foo.py",
  "error_code": "not_found",
  "path": "foo.py",
  "hint": "Check the file path. Use execute_command with 'ls' to find files.",
  "recoverable": true
}
```

**Tips:**
- Read a file BEFORE editing it to get exact content for `old_string`
- Use offset/limit to paginate large files
- Lines are 1-indexed
- Returns `encoding` so you know what the file uses

## write_file

Write or overwrite a file atomically. Returns a diff preview for existing files.

**Parameters:**
- `path` (string, required): File path
- `content` (string, required): Full file content
- `create_dirs` (boolean, optional): Create parent directories (default false)

**When to use:**
- Creating new files
- Complete file rewrites

**When NOT to use:**
- Small changes to existing files (use `edit_file` instead)

**Behavior:**
- Uses atomic writes (write to temp file, then move) to prevent corruption if the process crashes mid-write
- Retries up to 3 times on permission/lock errors with backoff
- Returns a unified diff showing exactly what changed
- Warns if the new content is identical to existing content
- Detects and preserves the file's original encoding

## edit_file

Find and replace an exact string in a file. The safest way to make targeted changes.

**Parameters:**
- `path` (string, required): File path
- `old_string` (string, required): Exact text to find
- `new_string` (string, required): Replacement text
- `replace_all` (boolean, optional): Replace all occurrences (default false, first only)

**Returns on success:**
```json
{
  "path": "/absolute/path/to/file",
  "replacements": 1,
  "total_occurrences": 1,
  "encoding": "utf-8",
  "diff": ["--- before", "+++ after", "@@ -10,3 +10,3 @@", ...],
  "diff_lines_added": 2,
  "diff_lines_removed": 1,
  "diff_truncated": false
}
```

**Error recovery — the tool helps you self-correct:**

If `old_string` is not found, the error includes:
- A fuzzy match suggestion showing the closest match in the file with similarity percentage
- A preview of the first 20 non-empty lines so you can see the actual content
- The detected encoding in case that's causing the mismatch

If `old_string` matches multiple locations, the error includes:
- Line numbers and surrounding context for each occurrence
- Instruction to add more context or use `replace_all=true`

**Critical rules:**
1. `old_string` must match EXACTLY — same indentation, whitespace, line endings
2. Include enough surrounding context (3-5 lines) to make the match unique
3. If you get "old_string not found", use the hint to self-correct and retry
4. If you get "found N times", add more context or use `replace_all=true`

**Example:**
```
old_string: "def hello():\n    print('hi')"
new_string: "def hello():\n    print('hello world')"
```

## Error Codes

All errors include `error_code`, `hint`, and `recoverable` fields:

| error_code | Meaning | recoverable |
|------------|---------|-------------|
| `access_denied` | Path is in a blocked directory | false |
| `not_found` | File does not exist | true |
| `too_large` | File exceeds 2MB limit | true |
| `permission_denied` | Insufficient file permissions | true |
| `disk_full` | No disk space remaining | true |
| `file_locked` | File locked by another process | true |
| `no_match` | old_string not found in file | true |
| `ambiguous_match` | old_string matches multiple locations | true |
| `identical_content` | old_string equals new_string | true |
| `encoding_error` | Could not detect file encoding | true |

When `recoverable` is true, the error includes a `hint` field with actionable guidance.

## Safety

- System directories (/etc, /usr, /bin, etc.) are blocked
- Secret files (.env, .ssh, .pem, .key) are blocked
- Max file size: 2MB
- Path traversal attacks are prevented via realpath resolution
- Atomic writes prevent corruption on crash
- Automatic retry (3 attempts) for transient errors
