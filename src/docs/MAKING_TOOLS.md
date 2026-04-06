# Making Tools

There are two ways to add tools to TALOS. Pick based on complexity.

## Quick Decision

| Need | Approach |
|------|----------|
| Wrap a shell command with parameters | Dynamic tool (no code) |
| Simple command alias | Dynamic tool (no code) |
| Complex logic, async, multi-step | Native Python tool |
| Needs its own Python module | Native Python tool |

---

## Path 1: Dynamic Tools (No Code)

Dynamic tools are command templates with named placeholders. They persist across restarts in `projects/dynamic_tools.json`.

### Creating via chat

Tell TALOS to make a tool:

> "Create a tool called grep_repo that searches the repo with ripgrep"

TALOS calls `create_tool` under the hood. You can also call it directly:

```json
{
  "name": "grep_repo",
  "description": "Search the repo with ripgrep",
  "command_template": "rg -n {pattern} {path}",
  "parameters": {
    "pattern": {"type": "string", "description": "Regex pattern"},
    "path": {"type": "string", "description": "Path or glob"}
  },
  "required": ["pattern", "path"],
  "timeout": 20
}
```

### How it works

1. `command_template` uses `{placeholder}` syntax for arguments.
2. Placeholders are automatically shell-quoted when the tool runs.
3. Any placeholder not in `parameters` is auto-added as a required string parameter.
4. A markdown guide file is generated at `tools/<name>.md`.
5. The tool definition is registered in `projects/dynamic_tools.json`.

### Name rules

- Lowercase letters, numbers, underscores only.
- Must start with a letter, minimum 3 characters.
- Cannot shadow built-in tool names (e.g. `execute_command`, `web_search`).

### Parameter types

Allowed: `string`, `number`, `integer`, `boolean`.

Parameters can be defined two ways:

Shorthand (string becomes description):
```json
{"pattern": "Regex pattern to search"}
```

Full descriptor:
```json
{"pattern": {"type": "string", "description": "Regex pattern to search"}}
```

### Managing dynamic tools

- `list_dynamic_tools` - list all dynamic tools
- `delete_tool` - remove a tool by name
- Update with `overwrite: true`

### Example: weather tool

```json
{
  "name": "check_weather",
  "description": "Get current weather for a city using curl",
  "command_template": "curl -s 'wttr.in/{city}?format=3'",
  "parameters": {
    "city": {"type": "string", "description": "City name"}
  },
  "required": ["city"],
  "timeout": 10
}
```

### Example: multi-parameter tool

```json
{
  "name": "docker_stats",
  "description": "Get resource usage for a Docker container",
  "command_template": "docker stats --no-stream {container}",
  "parameters": {
    "container": {"type": "string", "description": "Container name or ID"}
  },
  "required": ["container"],
  "timeout": 15
}
```

### Limitations

- Can only run shell commands.
- No async or multi-step logic.
- No access to TALOS internals (db, memory, send_func).
- Subject to the same terminal safety controls and timeouts as `execute_command`.

---

## Path 2: Native Python Tools

For tools that need custom logic, async operations, or access to TALOS internals.

### Step 1: Create the tool module

Create a Python file (e.g. `my_tool.py`) in the project root:

```python
import logging

logger = logging.getLogger("talos.my_tool")

async def execute(action: str, **kwargs) -> dict:
    if action == "do_something":
        result = kwargs.get("input", "")
        return {"ok": True, "result": result.upper()}
    return {"error": f"Unknown action: {action}"}
```

Follow existing module patterns: `terminal_tools.py`, `file_tools.py`, `spreadsheet_tools.py`, etc.

### Step 2: Register the tool schema

In `AI.py`, add the tool definition to the `_get_all_tools` function (around line 477). Add to the `tools` list:

```python
{
    "type": "function",
    "function": {
        "name": "my_tool_execute",
        "description": "Execute a custom action.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Action to perform"
                },
                "input": {
                    "type": "string",
                    "description": "Input data"
                }
            },
            "required": ["action"]
        }
    }
},
```

### Step 3: Add the import

At the top of `AI.py`, add:

```python
import my_tool
```

### Step 4: Wire up execution

In `_execute_tool_call` (around line 1279), add an `elif` branch:

```python
elif tool_name == "my_tool_execute":
    action = str(tool_args.get("action", "")).strip()
    if not action:
        return json.dumps({"error": "action is required"}, indent=2)
    result = await my_tool.execute(
        action=action,
        input=tool_args.get("input"),
    )
    return json.dumps(result, indent=2)
```

### Step 5: Create the tool guide

Create `tools/my_tool_execute.md` so the AI knows how to use the tool:

```markdown
# My Tool

Execute custom actions.

## Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string | Yes | Action to perform |
| input | string | No | Input data |

## Actions

- `do_something` - Transforms input to uppercase

## Example

```
my_tool_execute(action="do_something", input="hello")
```
```

### Step 6: Clear the tools cache

The tools guide is cached. Call `reload_clients()` or restart TALOS to pick up the new `.md` file.

---

## Tool Guide Files (`tools/*.md`)

Every tool (built-in or dynamic) should have a corresponding markdown file in `tools/`. These files are loaded into the AI's system prompt under `[Available Tools]`.

### What goes in a tool guide

- What the tool does and when to use it.
- All parameters with types, required/optional, and descriptions.
- Example invocations.
- Return value format.
- Error cases and recovery hints.
- Any safety notes or constraints.

### Conventions observed in existing guides

- Start with a `# Title` heading matching the tool name.
- Use `## Parameters` with either a table or bullet list.
- Include `## Example` sections with code blocks.
- Document return shapes with JSON blocks.
- Keep it concise. The AI reads all tool guides in every request.

### How guides are loaded

`AI.py:_load_tools_guide()` reads all `.md` files from the `tools/` directory, sorted alphabetically. Each file becomes an `### filename` section in the system prompt. The result is cached until invalidated (e.g. when dynamic tools are created or deleted).

---

## Checklist for Adding a Native Tool

1. [ ] Create the Python module with an async `execute` function
2. [ ] Add `import` to top of `AI.py`
3. [ ] Add tool schema to `_get_all_tools()` in `AI.py`
4. [ ] Add `elif` branch in `_execute_tool_call()` in `AI.py`
5. [ ] Create `tools/<tool_name>.md` guide
6. [ ] Restart TALOS or trigger reload
7. [ ] Test via Telegram or web chat
