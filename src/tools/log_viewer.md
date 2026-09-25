---
name: log_viewer
description: View and search recent log entries
command: "tail -n {line_count} {log_file} | grep --color '{search_term}'"
parameters: "log_file: string, line_count: string, search_term: string?"
---

# Log Viewer

Quickly view and filter log file contents.

**Usage:**
- `log_file`: Path to the log file (required)
- `line_count`: Number of recent lines to show (required, e.g., "100")
- `search_term`: Optional text to filter/highlight (optional)

**Features:**
- Shows last N lines of any log file
- Highlights matching search terms in color
- Fast and efficient for large files

Perfect for:
- Debugging application issues
- Monitoring recent activity
- Searching for specific errors or events
