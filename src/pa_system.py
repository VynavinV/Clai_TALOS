import logging
import os
import platform
import shutil
import subprocess

logger = logging.getLogger("talos.pa_system")

_MAX_TEXT_CHARS = 1200
_MAX_REPEAT = 5

_BACKEND_ALIASES = {
    "auto": "auto",
    "windows": "windows_sapi",
    "sapi": "windows_sapi",
    "windows_sapi": "windows_sapi",
    "say": "say",
    "spd": "spd_say",
    "spd-say": "spd_say",
    "spd_say": "spd_say",
    "espeak": "espeak",
}


def _normalize_text(text: str) -> str:
    cleaned = str(text or "").replace("\r", "\n")
    cleaned = " ".join(part for part in cleaned.splitlines() if part.strip())
    cleaned = " ".join(cleaned.split())
    return cleaned[:_MAX_TEXT_CHARS]


def _normalize_backend(backend: str) -> str:
    raw = str(backend or "auto").strip().lower()
    return _BACKEND_ALIASES.get(raw, "")


def _normalize_repeat(repeat: int | float | str) -> int:
    try:
        value = int(repeat)
    except Exception:
        value = 1
    return max(1, min(value, _MAX_REPEAT))


def _normalize_rate(rate: int | float | str | None) -> int | None:
    if rate is None:
        return None
    try:
        return int(rate)
    except Exception:
        return None


def _resolve_backends() -> dict[str, str]:
    backends: dict[str, str] = {}

    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell:
        backends["windows_sapi"] = powershell

    say_bin = shutil.which("say")
    if say_bin:
        backends["say"] = say_bin

    spd_bin = shutil.which("spd-say")
    if spd_bin:
        backends["spd_say"] = spd_bin

    espeak_bin = shutil.which("espeak")
    if espeak_bin:
        backends["espeak"] = espeak_bin

    return backends


def _preferred_backend_order() -> list[str]:
    system = platform.system().lower()
    if system == "windows":
        return ["windows_sapi", "spd_say", "espeak", "say"]
    if system == "darwin":
        return ["say", "spd_say", "espeak", "windows_sapi"]
    return ["spd_say", "espeak", "say", "windows_sapi"]


def _backend_sequence(requested_backend: str, available_backends: dict[str, str]) -> list[str]:
    if requested_backend and requested_backend != "auto":
        if requested_backend in available_backends:
            return [requested_backend]
        return []

    ordered: list[str] = []
    for name in _preferred_backend_order():
        if name in available_backends:
            ordered.append(name)
    return ordered


def _build_command(
    backend: str,
    backend_bin: str,
    text: str,
    voice: str,
    rate: int | None,
) -> list[str]:
    if backend == "windows_sapi":
        safe_text = text.replace("'", "''")
        safe_voice = voice.replace("'", "''") if voice else ""

        script_parts = [
            "Add-Type -AssemblyName System.Speech",
            "$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer",
        ]

        if safe_voice:
            script_parts.append(f"$speaker.SelectVoice('{safe_voice}')")

        if rate is not None:
            clamped = max(-10, min(rate, 10))
            script_parts.append(f"$speaker.Rate = {clamped}")

        script_parts.append(f"$speaker.Speak('{safe_text}')")
        script = "; ".join(script_parts)

        return [
            backend_bin,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ]

    if backend == "say":
        cmd = [backend_bin]
        if voice:
            cmd.extend(["-v", voice])
        if rate is not None:
            clamped = max(80, min(rate, 420))
            cmd.extend(["-r", str(clamped)])
        cmd.append(text)
        return cmd

    if backend == "spd_say":
        cmd = [backend_bin]
        if voice:
            cmd.extend(["-y", voice])
        if rate is not None:
            clamped = max(-100, min(rate, 100))
            cmd.extend(["-r", str(clamped)])
        cmd.append(text)
        return cmd

    if backend == "espeak":
        cmd = [backend_bin]
        if voice:
            cmd.extend(["-v", voice])
        if rate is not None:
            clamped = max(80, min(rate, 420))
            cmd.extend(["-s", str(clamped)])
        cmd.append(text)
        return cmd

    raise ValueError(f"Unknown backend: {backend}")


def _run_command(command: list[str], timeout_s: int) -> dict:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"Timed out after {timeout_s}s",
        }
    except FileNotFoundError:
        return {
            "ok": False,
            "error": "TTS backend executable not found",
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Failed to run TTS backend: {exc}",
        }

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return {
            "ok": False,
            "error": f"Backend exited with code {result.returncode}",
            "detail": detail[:400],
        }

    return {"ok": True}


def announce(
    text: str,
    repeat: int | float | str = 1,
    backend: str = "auto",
    voice: str = "",
    rate: int | float | str | None = None,
) -> dict:
    message = _normalize_text(text)
    if not message:
        return {"error": "text is required"}

    selected_backend = _normalize_backend(backend)
    if not selected_backend:
        return {"error": "Invalid backend. Use auto/windows_sapi/say/spd_say/espeak."}

    safe_voice = str(voice or "").strip()
    safe_repeat = _normalize_repeat(repeat)
    safe_rate = _normalize_rate(rate)

    available_backends = _resolve_backends()
    backend_queue = _backend_sequence(selected_backend, available_backends)

    if not backend_queue:
        available = sorted(available_backends.keys())
        if available:
            return {
                "error": "Requested backend is not available on this machine.",
                "requested_backend": selected_backend,
                "available_backends": available,
            }
        return {
            "error": (
                "No local TTS backend found. Install/enable one of: "
                "Windows SAPI (PowerShell), macOS say, Linux spd-say or espeak."
            ),
            "requested_backend": selected_backend,
        }

    timeout_s = max(10, min(120, 10 + (len(message) // 12)))
    attempts: list[dict] = []

    for backend_name in backend_queue:
        backend_bin = available_backends.get(backend_name, "")
        if not backend_bin:
            continue

        ok = True
        for idx in range(safe_repeat):
            command = _build_command(backend_name, backend_bin, message, safe_voice, safe_rate)
            result = _run_command(command, timeout_s)
            if not result.get("ok"):
                ok = False
                attempts.append(
                    {
                        "backend": backend_name,
                        "attempt": idx + 1,
                        "error": result.get("error", "unknown error"),
                        "detail": result.get("detail", ""),
                    }
                )
                logger.warning("PA backend failed (%s): %s", backend_name, result)
                break

        if ok:
            return {
                "ok": True,
                "spoken": message,
                "backend": backend_name,
                "repeat": safe_repeat,
                "rate": safe_rate,
                "voice": safe_voice,
                "host": platform.node(),
                "platform": platform.platform(),
            }

    return {
        "error": "Failed to speak on local speakers with available backends.",
        "attempts": attempts,
        "available_backends": sorted(available_backends.keys()),
    }
