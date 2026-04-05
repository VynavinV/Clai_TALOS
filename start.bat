@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

set "WEB_PORT=8080"

echo.
echo   ======================================
echo            Clai  TALOS
echo       Personal AI Assistant
echo   ======================================
echo.

:: ── Step 1: Ensure directories ───────────────────────────────────────

if not exist "projects" mkdir projects
if not exist "logs\web_uploads" mkdir "logs\web_uploads"
if not exist "logs\browser" mkdir "logs\browser"

:: ── Step 2: Find Python 3.10-3.13 ───────────────────────────────────

set "PYTHON="

:: Try py launcher first (most reliable on Windows)
where py >nul 2>nul
if %ERRORLEVEL% equ 0 (
    for /f "tokens=*" %%V in ('py -3.13 --version 2^>nul') do set "PYTHON=py -3.13"
    if not defined PYTHON (
        for /f "tokens=*" %%V in ('py -3.12 --version 2^>nul') do set "PYTHON=py -3.12"
    )
    if not defined PYTHON (
        for /f "tokens=*" %%V in ('py -3.11 --version 2^>nul') do set "PYTHON=py -3.11"
    )
    if not defined PYTHON (
        for /f "tokens=*" %%V in ('py -3.10 --version 2^>nul') do set "PYTHON=py -3.10"
    )
)

:: Fallback to python in PATH
if not defined PYTHON (
    where python >nul 2>nul
    if %ERRORLEVEL% equ 0 (
        for /f "tokens=2 delims= " %%V in ('python --version 2^>nul') do (
            echo %%V | findstr /r "^3\.1[0-3]\." >nul && set "PYTHON=python"
        )
    )
)

if not defined PYTHON (
    echo [FAIL] Python 3.10-3.13 not found.
    echo.
    echo   Download Python from: https://www.python.org/downloads/
    echo   Make sure to check "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)

echo [  OK] Python found: %PYTHON%

:: ── Step 3: Create venv + install deps ───────────────────────────────

if not exist "venv" (
    echo [setup] Creating virtual environment...
    %PYTHON% -m venv venv
    if %ERRORLEVEL% neq 0 (
        echo [FAIL] Could not create virtual environment.
        pause
        exit /b 1
    )
)

:: Install deps
if exist "requirements.txt" (
    echo [setup] Installing dependencies...
    venv\Scripts\pip install -q --upgrade pip 2>nul
    venv\Scripts\pip install -q -r requirements.txt 2>nul
)

echo [  OK] Virtual environment ready

:: ── Step 4: Run setup ────────────────────────────────────────────────

venv\Scripts\python setup.py
if %ERRORLEVEL% neq 0 (
    echo [FAIL] Setup failed.
    pause
    exit /b 1
)

echo [  OK] Configuration checked

:: ── Step 5: Tailscale Funnel (best-effort) ───────────────────────────

where tailscale >nul 2>nul
if %ERRORLEVEL% equ 0 (
    tailscale status >nul 2>nul
    if %ERRORLEVEL% equ 0 (
        tailscale funnel --bg %WEB_PORT% >nul 2>nul
        if %ERRORLEVEL% equ 0 (
            echo [  OK] Tailscale Funnel active
        )
    )
) else (
    echo [ info] Tailscale not installed. Install from: https://tailscale.com/download
    echo [ info] You can set this up later from the dashboard.
)

:: ── Step 6: Open browser + start bot ─────────────────────────────────

echo.
echo   Ready!
echo   Dashboard: http://localhost:%WEB_PORT%
echo.

start "" "http://localhost:%WEB_PORT%"

venv\Scripts\python telegram_bot.py
pause
