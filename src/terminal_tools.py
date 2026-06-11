import os
import sys
import json
import signal
import asyncio
import logging
import platform
import shutil
import time
from datetime import datetime, timezone

import app_paths

try:
    import docker
    DOCKER_AVAILABLE = True
except ImportError:
    DOCKER_AVAILABLE = False

LOG_DIR = app_paths.logs_dir()
CONFIG_FILE = app_paths.terminal_config_path()

os.makedirs(LOG_DIR, exist_ok=True)

audit_logger = logging.getLogger("talos.audit")
audit_logger.setLevel(logging.INFO)
audit_handler = logging.FileHandler(os.path.join(LOG_DIR, "audit.log"))
audit_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
audit_logger.addHandler(audit_handler)

DANGEROUS_COMMANDS = {
    "rm", "dd", "mkfs", "shutdown", "reboot", "halt", "poweroff",
    "init", "systemctl", "chmod", "chown", "mv", "cp",
    "del", "rmdir", "rd", "format", "diskpart", "reg",
}

DANGEROUS_PATTERNS = [
    "rm -rf /", "rm -rf /*", "dd if=", "mkfs", "> /dev/", "chmod 777",
    "chown root", "shutdown", "reboot", "halt",
    "del /s /q", "rd /s /q", "rmdir /s /q", "format ",
    "reg delete", "diskpart",
]


class TerminalExecutor:
    def __init__(self, config: dict | None = None):
        if config is None:
            config = self._load_config()
        
        self.sandbox_mode = config.get("sandbox_mode", "docker")
        self.require_confirmation = config.get("require_confirmation", True)
        self.max_commands_per_minute = config.get("max_commands_per_minute", 10)
        self.default_timeout = config.get("default_timeout", 30)
        self.dangerous_commands = set(config.get("dangerous_commands", list(DANGEROUS_COMMANDS)))
        
        self.command_timestamps: list[float] = []
        self.docker_client = None
        
        if self.sandbox_mode == "docker":
            if not DOCKER_AVAILABLE:
                raise RuntimeError("Docker library not installed. Run: pip install docker")
            try:
                self.docker_client = docker.from_env()
                self.docker_client.ping()
            except Exception as e:
                raise RuntimeError(f"Docker not available: {e}")
    
    def _load_config(self) -> dict:
        if os.path.isfile(CONFIG_FILE):
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        return {
            "sandbox_mode": "native",
            "require_confirmation": True,
            "max_commands_per_minute": 10,
            "default_timeout": 30,
            "dangerous_commands": list(DANGEROUS_COMMANDS)
        }
    
    def _is_rate_limited(self) -> bool:
        now = time.time()
        self.command_timestamps = [t for t in self.command_timestamps if now - t < 60]
        if len(self.command_timestamps) >= self.max_commands_per_minute:
            return True
        self.command_timestamps.append(now)
        return False
    
    def _is_dangerous(self, command: str) -> bool:
        cmd_lower = command.lower().strip()
        
        cmd_parts = cmd_lower.split()
        if cmd_parts and cmd_parts[0] in self.dangerous_commands:
            return True
        
        for pattern in DANGEROUS_PATTERNS:
            if pattern in cmd_lower:
                return True
        
        return False
    
    def _log_audit(self, command: str, status: str, result: dict | None = None):
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log_entry = {
            "timestamp": timestamp,
            "command": command,
            "status": status,
            "sandbox_mode": self.sandbox_mode
        }
        if result:
            log_entry["exit_code"] = result.get("exit_code")
            if "error" in result:
                log_entry["error"] = result["error"]
        
        audit_logger.info(json.dumps(log_entry))
    
    async def execute(
        self,
        command: str,
        timeout: int | None = None,
        require_confirmation: bool | None = None
    ) -> dict:
        if timeout is None:
            timeout = self.default_timeout
        
        if require_confirmation is None:
            require_confirmation = self.require_confirmation
        
        if self._is_rate_limited():
            error_msg = "Rate limit exceeded. Too many commands."
            self._log_audit(command, "rate_limited", {"error": error_msg})
            return {"error": error_msg, "exit_code": -1}
        
        if require_confirmation and self._is_dangerous(command):
            self._log_audit(command, "confirmation_required")
            return {
                "status": "confirmation_required",
                "command": command,
                "message": "This command requires user confirmation",
                "dangerous": True
            }
        
        try:
            if self.sandbox_mode == "docker":
                result = await self._execute_docker(command, timeout)
            elif self.sandbox_mode == "firejail":
                result = await self._execute_firejail(command, timeout)
            else:
                result = await self._execute_native(command, timeout)
            
            self._log_audit(command, "success", result)
            return result
        
        except asyncio.TimeoutError:
            error_result = {"error": "Command timed out", "exit_code": -1}
            self._log_audit(command, "timeout", error_result)
            return error_result
        except Exception as e:
            error_result = {"error": str(e), "exit_code": 1}
            self._log_audit(command, "error", error_result)
            return error_result
    
    async def _kill_process(self, process: asyncio.subprocess.Process):
        if sys.platform == "win32":
            import subprocess as sp
            try:
                sp.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                       capture_output=True, timeout=5)
            except Exception:
                process.kill()
        else:
            try:
                pgid = os.getpgid(process.pid)
                if pgid != os.getpid():
                    os.killpg(pgid, signal.SIGKILL)
                else:
                    process.kill()
            except (ProcessLookupError, PermissionError, OSError):
                process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except (asyncio.TimeoutError, Exception):
            pass

    async def _execute_native(self, command: str, timeout: int) -> dict:
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                shell=True
            )
            
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout
            )
            
            return {
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
                "exit_code": process.returncode
            }
        except asyncio.TimeoutError:
            await self._kill_process(process)
            raise
    
    async def _execute_docker(self, command: str, timeout: int) -> dict:
        if not self.docker_client:
            raise RuntimeError("Docker client not initialized")
        
        try:
            container = self.docker_client.containers.run(
                "alpine:latest",
                command=["sh", "-c", command],
                detach=True,
                mem_limit="512m",
                cpu_quota=50000,
                network_disabled=True,
                remove=False,
                timeout=timeout + 5
            )
            
            start_time = time.time()
            while time.time() - start_time < timeout:
                container.reload()
                if container.status == "exited":
                    break
                await asyncio.sleep(0.1)
            else:
                container.kill()
                container.remove()
                raise asyncio.TimeoutError()
            
            stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")
            exit_code = container.attrs["State"]["ExitCode"]
            
            container.remove()
            
            return {
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": exit_code
            }
        except Exception as e:
            if "container" in locals():
                try:
                    container.remove(force=True)
                except Exception:
                    pass
            raise
    
    async def _execute_firejail(self, command: str, timeout: int) -> dict:
        if platform.system() != "Linux":
            raise RuntimeError("Firejail sandbox is only available on Linux")
        if not shutil.which("firejail"):
            raise RuntimeError("Firejail not installed")
        
        firejail_cmd = [
            "firejail",
            "--noprofile",
            "--private",
            "--net=none",
            f"--timeout={timeout}",
            "--",
            "sh", "-c", command
        ]
        
        try:
            process = await asyncio.create_subprocess_exec(
                *firejail_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout + 5
            )
            
            return {
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
                "exit_code": process.returncode
            }
        except asyncio.TimeoutError:
            await self._kill_process(process)
            raise
    
    async def execute_workflow(self, steps: list[dict]) -> dict:
        results = []
        
        for i, step in enumerate(steps):
            command = step.get("command")
            if not command:
                results.append({
                    "step": i,
                    "error": "No command specified",
                    "exit_code": 1
                })
                continue
            
            timeout = step.get("timeout", self.default_timeout)
            result = await self.execute(command, timeout=timeout)
            results.append({
                "step": i,
                "command": command,
                **result
            })
            
            if result.get("exit_code", 0) != 0:
                return {
                    "status": "workflow_stopped",
                    "step": i,
                    "reason": "command_failed",
                    "results": results
                }
            
            condition = step.get("condition")
            if condition:
                if not self._check_condition(condition, result):
                    return {
                        "status": "workflow_stopped",
                        "step": i,
                        "reason": "condition_not_met",
                        "results": results
                    }
        
        return {
            "status": "workflow_complete",
            "results": results
        }
    
    def _check_condition(self, condition: str, result: dict) -> bool:
        if condition == "success":
            return result.get("exit_code", 1) == 0
        elif condition == "failure":
            return result.get("exit_code", 0) != 0
        elif condition == "output_contains":
            return bool(result.get("stdout", ""))
        
        return True


_executor: TerminalExecutor | None = None


def get_executor() -> TerminalExecutor:
    global _executor
    if _executor is None:
        _executor = TerminalExecutor()
    return _executor


async def execute_command(command: str, timeout: int | None = None) -> dict:
    return await get_executor().execute(command, timeout=timeout)


async def execute_workflow(steps: list[dict]) -> dict:
    return await get_executor().execute_workflow(steps)
