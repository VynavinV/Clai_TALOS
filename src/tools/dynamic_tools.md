# Dynamic Tool Builder

Create reusable tools at runtime and persist them across restarts.

## create_tool

Create or update a dynamic tool backed by a shell command template.

Parameters:
- `name` (string, required): Tool name in `lowercase_with_underscores`.
- `description` (string, required): Clear summary of what the tool does.
- `command_template` (string, required): Shell command template. Use placeholders like `{query}`.
- `parameters` (object, optional): Argument definitions map.
- `required` (array, optional): Required argument names.
- `timeout` (number, optional): Default timeout in seconds.
- `guide` (string, optional): Extra notes added to the generated tool guide file.
- `overwrite` (boolean, optional): Replace existing dynamic tool with the same name.

Example:
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

## list_dynamic_tools

List all dynamic tools currently registered.

## delete_tool

Delete one dynamic tool by name.

Parameters:
- `name` (string, required): Dynamic tool name to remove.

## Notes

- Dynamic tools are stored in `projects/dynamic_tools.json`.
- A markdown guide for each dynamic tool is generated in `tools/<name>.md`.
- Placeholders in `command_template` become required inputs unless explicitly removed by updating the tool.
- Dynamic tools execute through the same terminal safety controls and timeout system as `execute_command`.
