# Terminal Agent Feature

Clai TALOS now includes terminal command execution capabilities with sandboxed security.

## Features

✅ **Native ZhipuAI Function Calling** - Uses ZhipuAI's built-in tool support  
✅ **Sandboxed Execution** - Docker containers for isolation (optional native mode)  
✅ **Multi-Step Workflows** - Chain commands with conditional execution  
✅ **Automatic Safety** - Dangerous commands require confirmation  
✅ **Audit Logging** - All commands logged for security  
✅ **Rate Limiting** - Prevent abuse with configurable limits  
✅ **Automated Setup** - Zero manual configuration required  

## Quick Start

### 1. Run Setup
```bash
python setup.py
```

When prompted:
- Select "Enable terminal command execution" (Step 8)
- Choose sandbox mode:
  - **Yes** (default) - Docker sandbox (safest, isolated)
  - **No** - Native mode with sudo access

### 2. Restart Bot
```bash
source venv/bin/activate
python telegram_bot.py
```

### 3. Use in Telegram
Just ask the bot to execute commands:
- "Check disk space"
- "Show running processes"
- "Find large files in /var/log"
- "Deploy the app" (multi-step workflow)

## Architecture

```
User Message → ZhipuAI (with tools) → Function Call → Terminal Executor (sandboxed) → Result → AI Response
```

### Components

**terminal_tools.py** - Core execution engine
- `TerminalExecutor` class with sandboxing support
- Docker and native execution modes
- Rate limiting and audit logging
- Dangerous command detection

**AI.py** - Function calling integration
- `execute_command` tool for single commands
- `execute_workflow` tool for multi-step operations
- Automatic tool result processing

**Dockerfile.sandbox** - Isolated execution environment
- Alpine Linux base
- Common utilities pre-installed
- Resource limits (CPU, memory, network)

**setup.py** - Simplified configuration
- Simple y/n choice for sandboxed execution
- Automatic Docker setup
- Native mode with sudo configuration
- Security settings auto-configuration

## Security Features

### Sandboxing
- **Docker**: Commands run in isolated containers with:
  - No network access
  - 512MB memory limit
  - 50% CPU limit
  - Automatic cleanup

- **Native**: Direct execution with:
  - Sudoers configuration for passwordless sudo
  - Command whitelisting (optional)
  - Audit logging

### Command Confirmation
Dangerous commands automatically require user confirmation:
- `rm` - File deletion
- `dd` - Disk operations
- `mkfs` - Filesystem creation
- `shutdown`, `reboot` - System control
- And more...

### Audit Logging
All commands logged to `logs/audit.log`:
```json
{
  "timestamp": "2026-04-03T18:47:12Z",
  "command": "df -h",
  "status": "success",
  "exit_code": 0,
  "sandbox_mode": "docker"
}
```

### Rate Limiting
- Default: 10 commands per minute
- Configurable in `terminal_config.json`
- Prevents accidental or malicious abuse

## Configuration

Configuration stored in `terminal_config.json`:

```json
{
  "sandbox_mode": "docker",
  "require_confirmation": true,
  "audit_logging": true,
  "max_commands_per_minute": 10,
  "default_timeout": 30,
  "dangerous_commands": ["rm", "dd", "mkfs", ...]
}
```

### Changing Configuration

Edit `terminal_config.json` or re-run setup:
```bash
python setup.py
# Select "Reconfigure" when prompted
```

## Tool Reference

### execute_command

Execute a single terminal command.

**Parameters:**
- `command` (string, required): Command to execute
- `timeout` (number, optional): Max execution time in seconds (default: 30)

**Returns:**
```json
{
  "stdout": "command output",
  "stderr": "error output", 
  "exit_code": 0
}
```

**Example:**
```python
# AI will call:
execute_command(command="df -h", timeout=10)
```

### execute_workflow

Execute multiple commands in sequence.

**Parameters:**
- `steps` (array): List of command steps
  - `command` (string): Command to execute
  - `timeout` (number, optional): Step timeout
  - `condition` (string, optional): "success", "failure", or "output_contains"

**Returns:**
```json
{
  "status": "workflow_complete",
  "results": [
    {"step": 0, "command": "git pull", "exit_code": 0, ...},
    {"step": 1, "command": "npm install", "exit_code": 0, ...}
  ]
}
```

**Example:**
```python
# AI will call:
execute_workflow(steps=[
  {"command": "git pull", "condition": "success"},
  {"command": "npm install", "condition": "success"},
  {"command": "npm test", "condition": "success"}
])
```

## Common Use Cases

### System Monitoring
```
User: "Check if we're running out of disk space"
Bot: [executes df -h]
     "Yeah, you've got 87% used on /var. Clean up old logs or this is going to be a problem."
```

### File Operations
```
User: "Find all log files larger than 100MB"
Bot: [executes find /var/log -size +100M]
     "Found 3 files: syslog (150MB), kern.log (120MB), auth.log (105MB)"
```

### Multi-Step Workflows
```
User: "Deploy the latest version"
Bot: [executes workflow: git pull → npm install → npm test → systemctl restart]
     "Deployed successfully. All tests passed, service restarted."
```

### Process Management
```
User: "Is nginx running?"
Bot: [executes ps aux | grep nginx]
     "Yeah, nginx is running. PID 1234, using 45MB RAM."
```

## Troubleshooting

### Docker not available
```
Error: Docker not available
Solution: 
  1. Install Docker Desktop (Mac) or Docker Engine (Linux)
  2. Start Docker
  3. Re-run setup.py
```

### Commands require sudo password
```
Error: sudo: no tty present and askpass program specified
Solution:
  1. Run setup.py
  2. Choose "No" for sandboxed execution
  3. Select "Configure passwordless sudo" when prompted
  4. Or use Docker mode (recommended)
```

### Rate limit exceeded
```
Error: Rate limit exceeded. Too many commands.
Solution:
  1. Wait 60 seconds
  2. Or increase max_commands_per_minute in terminal_config.json
```

### Command timeout
```
Error: Command timed out
Solution:
  1. Increase timeout parameter
  2. Or optimize the command
```

## Safety Best Practices

1. **Use Docker mode** - Safest option with full isolation
2. **Review dangerous commands** - Bot will ask for confirmation
3. **Check audit logs** - Monitor what commands are being executed
4. **Start with read-only** - Test with safe commands first
5. **Set appropriate rate limits** - Prevent accidental spam

## Advanced Usage

### Custom Docker Image

Edit `Dockerfile.sandbox` to add custom tools:
```dockerfile
RUN apk add --no-cache \
    your-tool-here
```

Rebuild:
```bash
docker build -t talos-sandbox -f Dockerfile.sandbox .
```

### Command Whitelisting

For extra security, modify `terminal_tools.py`:
```python
ALLOWED_COMMANDS = {"ls", "df", "ps", "cat", "grep"}

def _is_allowed(self, command: str) -> bool:
    cmd_name = command.split()[0]
    return cmd_name in ALLOWED_COMMANDS
```

### Custom Audit Handler

Add custom audit handling in `terminal_tools.py`:
```python
def _log_audit(self, command: str, status: str, result: dict):
    # Your custom logging here
    send_to_slack(f"Command executed: {command}")
    super()._log_audit(command, status, result)
```

## Files Created

- `terminal_tools.py` - Core execution engine
- `Dockerfile.sandbox` - Docker image definition
- `terminal_config.json` - Configuration (auto-generated)
- `logs/audit.log` - Audit trail
- `requirements.txt` - Dependencies (updated)

## Files Modified

- `AI.py` - Added function calling support
- `setup.py` - Simplified terminal agent setup
- `toolguide.md` - Tool documentation
- `system_prompt.md` - Tool usage guidance

## Support

For issues or questions:
1. Check audit logs: `logs/audit.log`
2. Verify configuration: `terminal_config.json`
3. Test Docker: `docker run alpine:latest echo "test"`
4. Re-run setup: `python setup.py`

## License

Part of Clai TALOS project.
