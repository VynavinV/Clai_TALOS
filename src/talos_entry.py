import asyncio
import subprocess
import sys
from pathlib import Path


def _run_windows_headless_start() -> int:
    exe_dir = Path(sys.executable).resolve().parent
    candidates = [
        exe_dir / "start.bat",
        exe_dir / "_internal" / "start.bat",
    ]
    start_bat = next((path for path in candidates if path.is_file()), None)

    if start_bat is None:
        searched = ", ".join(str(path) for path in candidates)
        print(f"[fail] start.bat not found. Searched: {searched}")
        return 2

    print("[info] Running startup command: clai ./start.bat --headless")
    return subprocess.call(
        ["cmd.exe", "/c", str(start_bat), "--headless"],
        cwd=str(start_bat.parent),
    )


if __name__ == "__main__":
    if sys.platform == "win32" and bool(getattr(sys, "frozen", False)):
        exit_code = _run_windows_headless_start()
        raise SystemExit(exit_code)

    import telegram_bot

    asyncio.run(telegram_bot.main())
