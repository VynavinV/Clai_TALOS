# Tools Reference

Tools are loaded from the `tools/` folder. Each `.md` file defines a tool with its parameters and usage.

## Available Tools

| Tool | Description |
|------|-------------|
| **terminal** | Execute shell commands with sandboxing support |
| **memory** | Persistent memory storage with auto-retrieval |
| **cron** | Schedule recurring commands using cron syntax |
| **websearch** | Search the web for current information |
| **firecrawl** | Scrape and extract content from web pages |
| **voice** | Send and receive voice messages on Telegram |

## Tool Files

Detailed documentation is in `tools/*.md`:
- `tools/terminal.md` - Command execution and workflows
- `tools/memory.md` - Memory save/search/list/delete operations
- `tools/cron.md` - Cron scheduling (schedule/list/remove)
- `tools/websearch.md` - Web search for current information
- `tools/firecrawl.md` - Web scraping and content extraction
- `tools/voice.md` - Voice message transcription and TTS

## Adding New Tools

1. Create `tools/your_tool.md` with tool documentation
2. Add function definition in `AI.py` `_get_all_tools()`
3. Add handler in `AI.py` `_execute_tool_call()`
### Best Practices
1. **Start simple**: Use single commands before trying workflows
2. **Check first**: Use read-only commands to verify before making changes
3. **Be specific**: Provide exact paths and options
4. **Handle errors**: Check exit_code in responses

### Security Notes
- Commands run in isolated sandbox (Docker container by default)
- No persistent state between commands
- Network access disabled in sandbox
- Resource limits applied (CPU, memory, time)

### Common Use Cases

**System Monitoring:**
- `top -n 1` or `htop` - Process overview
- `df -h` - Disk usage
- `free -m` - Memory usage
- `uptime` - System uptime

**File Operations:**
- `ls -la /path` - List files
- `find /path -name "*.ext"` - Find files
- `cat /path/to/file` - Read file
- `grep "pattern" /path/to/file` - Search in file

**Network:**
- `curl -I https://example.com` - Check website
- `ping -c 3 hostname` - Test connectivity
- `netstat -tuln` - List open ports

**Process Management:**
- `ps aux | grep process` - Find process
- `kill -9 PID` - Kill process (requires confirmation)

**Web Search:**
- `web_search(query="latest news")` - Search the web
- `web_search(query="Python 3.14 features", scope="academic")` - Academic search
- `web_search(query="weather", location="New York")` - Location-aware search

**Web Scraping:**
- `scrape_url(url="https://example.com")` - Scrape a webpage
- `scrape_url(url="https://docs.python.org", formats=["markdown", "links"])` - Get markdown and links
- `scrape_url(url="https://example.com", formats=["screenshot"])` - Take a screenshot

## Limitations
- No interactive commands (no vim, nano in interactive mode)
- No GUI applications
- Commands timeout after 30 seconds by default
- Some system-level operations may be restricted by sandbox
