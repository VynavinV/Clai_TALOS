@echo off
echo Starting Clai TALOS...
echo.

rem Ensure projects directory exists
if not exist "%~dp0projects" mkdir "%~dp0projects"

python setup.py
if %ERRORLEVEL% neq 0 (
    echo Setup failed. Please fix errors and try again.
    pause
    exit /b 1
)

rem Setup Tailscale Funnel
set "WEB_PORT=8080"
if defined WEB_PORT set WEB_PORT=%WEB_PORT%
where tailscale >nul 2>nul
if %ERRORLEVEL% equ 0 (
    tailscale status >nul 2>nul
    if %ERRORLEVEL% equ 0 (
        echo [tailscale] Setting up Funnel on port %WEB_PORT%...
        tailscale funnel --bg %WEB_PORT% >nul 2>nul
        if %ERRORLEVEL% equ 0 (
            echo [tailscale] Funnel active
        ) else (
            echo [tailscale] Funnel setup failed
        )
    ) else (
        echo [tailscale] Not connected. Run: tailscale up
    )
) else (
    echo [tailscale] Not installed. Install from https://tailscale.com/download
)

echo.
echo Starting bot...
echo.

venv\Scripts\python telegram_bot.py
pause
