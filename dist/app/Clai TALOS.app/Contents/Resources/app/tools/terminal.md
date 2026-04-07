# Terminal Execution

Execute terminal commands in a sandboxed environment.

## execute_command

Execute a single terminal command.

**Parameters:**
- `command` (string, required): The terminal command to execute
- `timeout` (number, optional): Maximum execution time in seconds (default: 30)

**Returns:**
```json
{
  "stdout": "command output",
  "stderr": "error output",
  "exit_code": 0
}
```

## execute_workflow

Execute multiple commands in sequence.

**Parameters:**
- `steps` (array): List of command steps
  - `command` (string): Command to execute
  - `timeout` (number, optional): Timeout for this step
  - `condition` (string, optional): "success", "failure", or "output_contains"

**Example:**
```json
{
  "steps": [
    {"command": "git pull", "condition": "success"},
    {"command": "npm install"},
    {"command": "npm test"}
  ]
}
```

## Safety

- Dangerous commands may require confirmation
- Automatic timeout prevents hanging
- Rate limiting (max 10 commands/minute)
- All commands logged

## Common Commands

- `ls -la /path` - List files
- `cat /path/to/file` - Read file
- `grep "pattern" file` - Search in file
- `find /path -name "*.ext"` - Find files
- `df -h` - Disk usage
- `ps aux` - Process list
