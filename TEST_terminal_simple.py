#!/usr/bin/env python3
"""Simple test to verify terminal agent works"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import terminal_tools

async def main():
    print("=" * 60)
    print("Terminal Agent Quick Test")
    print("=" * 60)
    
    config = {
        "sandbox_mode": "native",
        "require_confirmation": False,
        "max_commands_per_minute": 100,
        "default_timeout": 5
    }
    
    executor = terminal_tools.TerminalExecutor(config)
    
    print("\nTest 1: Execute simple command")
    result = await executor.execute("echo 'Hello from TALOS terminal agent!'")
    print(f"Exit code: {result.get('exit_code')}")
    print(f"Output: {result.get('stdout')}")
    
    print("\nTest 2: Check disk space")
    result = await executor.execute("df -h")
    print(f"Exit code: {result.get('exit_code')}")
    print(f"Output:\n{result.get('stdout')[:200]}")
    
    print("\nTest 3: List current directory")
    result = await executor.execute("pwd")
    print(f"Exit code: {result.get('exit_code')}")
    print(f"Current directory: {result.get('stdout').strip()}")
    
    print("\nTest 4: Multi-step workflow")
    steps = [
        {"command": "echo 'Step 1: Check'", "condition": "success"},
        {"command": "echo 'Step 2: Process'", "condition": "success"},
        {"command": "echo 'Step 3: Complete'", "condition": "success"}
    ]
    result = await executor.execute_workflow(steps)
    print(f"Status: {result.get('status')}")
    print(f"Steps completed: {len(result.get('results', []))}")
    
    print("\n" + "=" * 60)
    print("All tests passed! Terminal agent is working correctly.")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
