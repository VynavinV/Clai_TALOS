@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0src"

set "WEB_PORT=8080"
set "HEADLESS=0"
set "INSTALL_SERVICE=0"

if /I "%~1"=="--headless" set "HEADLESS=1"
if /I "%~1"=="--install-service" (
    set "HEADLESS=1"
    set "INSTALL_SERVICE=1"
)

if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        if /I "%%A"=="WEB_PORT" set "WEB_PORT=%%B"
    )
)
set "WEB_PORT=%WEB_PORT:\"=%"
set "WEB_PORT=%WEB_PORT: =%"

echo.
echo   ======================================
echo            Clai  TALOS
echo       Personal AI Assistant
echo   ======================================
echo.

:: ── Step 1: Find or install Python 3.10-3.13 ────────────────────────

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

:: Auto-install Python via winget if not found
if not defined PYTHON (
    echo [info] Python 3.10-3.13 not found. Attempting auto-install...
    where winget >nul 2>nul
    if !ERRORLEVEL! neq 0 (
        echo [FAIL] Python not found and winget is not available.
        echo.
        echo   Download Python from: https://www.python.org/downloads/
        echo   Make sure to check "Add Python to PATH" during install.
        echo.
        pause
        exit /b 1
    )
    echo [setup] Installing Python 3.12 via winget...
    winget install --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements --silent
    if !ERRORLEVEL! neq 0 (
        echo [FAIL] Could not install Python via winget.
        echo   Download manually: https://www.python.org/downloads/
        pause
        exit /b 1
    )
    echo [  OK] Python installed. Refreshing PATH...

    :: Refresh PATH — find the newly installed Python
    set "PYPATH=%LOCALAPPDATA%\Programs\Python\Python312"
    if exist "!PYPATH!\python.exe" (
        set "PATH=!PYPATH!;!PYPATH!\Scripts;!PATH!"
    )

    :: Retry py launcher
    where py >nul 2>nul
    if !ERRORLEVEL! equ 0 (
        for /f "tokens=*" %%V in ('py -3.12 --version 2^>nul') do set "PYTHON=py -3.12"
    )
    if not defined PYTHON (
        :: Try direct path
        if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
            set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        )
    )
    if not defined PYTHON (
        echo [FAIL] Python was installed but could not be found. Restart your terminal and try again.
        pause
        exit /b 1
    )
    echo [  OK] Python found: !PYTHON!
) else (
    echo [  OK] Python found: %PYTHON%
)

:: ── Step 2: Ensure directories ───────────────────────────────────────

if not exist "projects" mkdir projects
if not exist "logs\web_uploads" mkdir "logs\web_uploads"
if not exist "logs\browser" mkdir "logs\browser"

:: ── Step 3: Check/install Chromium ───────────────────────────────────

where chrome >nul 2>nul && goto :chromium_done
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" goto :chromium_done
if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" goto :chromium_done
if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" goto :chromium_done

:: Check for any browser
if exist "%ProgramFiles%\Mozilla Firefox\firefox.exe" goto :chromium_done
if exist "%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe" goto :chromium_done
if exist "%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe" goto :chromium_done

echo [info] No browser found. Installing Chromium via winget...
where winget >nul 2>nul
if %ERRORLEVEL% equ 0 (
    winget install --id Google.Chrome --accept-source-agreements --accept-package-agreements --silent >nul 2>nul
    if !ERRORLEVEL! equ 0 (
        echo [  OK] Chromium installed.
    ) else (
        echo [warn] Could not install Chromium. Browser tools may not work.
    )
) else (
    echo [warn] winget not found. Install a browser manually for browser tools.
)

:chromium_done

:: ── Step 4: Create venv + install deps ───────────────────────────────

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
    venv\Scripts\python -m pip install --upgrade pip >nul 2>nul
    venv\Scripts\python -m pip install -r requirements.txt
    if %ERRORLEVEL% neq 0 (
        echo [FAIL] Could not install requirements.
        pause
        exit /b 1
    )
)

echo [  OK] Virtual environment ready

:: ── Step 5: Run setup ────────────────────────────────────────────────

venv\Scripts\python setup.py
if %ERRORLEVEL% neq 0 (
    echo [FAIL] Setup failed.
    pause
    exit /b 1
)

echo [  OK] Configuration checked

:: ── Step 6: Tailscale Funnel (best-effort) ───────────────────────────

set "TAILSCALE_BIN="
where tailscale >nul 2>nul
if %ERRORLEVEL% equ 0 set "TAILSCALE_BIN=tailscale"

if not defined TAILSCALE_BIN if exist "%ProgramFiles%\Tailscale\tailscale.exe" set "TAILSCALE_BIN=%ProgramFiles%\Tailscale\tailscale.exe"
if not defined TAILSCALE_BIN if exist "%ProgramFiles(x86)%\Tailscale\tailscale.exe" set "TAILSCALE_BIN=%ProgramFiles(x86)%\Tailscale\tailscale.exe"
if not defined TAILSCALE_BIN if exist "%LOCALAPPDATA%\Programs\Tailscale\tailscale.exe" set "TAILSCALE_BIN=%LOCALAPPDATA%\Programs\Tailscale\tailscale.exe"
if not defined TAILSCALE_BIN if exist "%LOCALAPPDATA%\Tailscale\tailscale.exe" set "TAILSCALE_BIN=%LOCALAPPDATA%\Tailscale\tailscale.exe"

if defined TAILSCALE_BIN (
    "%TAILSCALE_BIN%" status >nul 2>nul
    if %ERRORLEVEL% equ 0 (
        "%TAILSCALE_BIN%" funnel --bg %WEB_PORT% >nul 2>nul
        if %ERRORLEVEL% equ 0 (
            echo [  OK] Tailscale Funnel active
        )
    )
) else (
    echo [ info] Tailscale not installed. Install from: https://tailscale.com/download
    echo [ info] If you just installed it, restart this terminal and run start.bat again.
    echo [ info] You can set this up later from the dashboard.
)

:: ── Step 7: Install service or run foreground ────────────────────────

if "%INSTALL_SERVICE%"=="1" goto :do_install_service

:: Check if already running as a service
schtasks /query /tn "ClaiTALOS" >nul 2>nul
if %ERRORLEVEL% equ 0 (
    echo.
    echo [info] Clai TALOS is already running as a background service.
    echo [info] Dashboard: http://localhost:%WEB_PORT%
    echo [info] Use: scripts\clai.bat status
    echo [info] To run in foreground: stop the service first (scripts\clai.bat stop)
    start "" "http://localhost:%WEB_PORT%"
    exit /b 0
)

if "%HEADLESS%"=="1" goto :run_foreground

:: Ask user if they want background mode
echo.
echo   Run Clai TALOS in the background?
echo.
echo     Y: starts now and every time you log in (recommended)
echo     N: runs here in this terminal and stops when you close it
echo.
set /p "BG_CHOICE=  [Y/n]: "

if /I "!BG_CHOICE!"=="n" goto :run_foreground
if /I "!BG_CHOICE!"=="" goto :do_install_service

:do_install_service
echo.
echo [setup] Installing background service...
call "%~dp0scripts\install_service.bat" /install
if %ERRORLEVEL% equ 0 (
    echo.
    echo   ======================================
    echo    Clai TALOS is running in the background
    echo   ======================================
    echo.
    echo   Dashboard:  http://localhost:%WEB_PORT%
    echo.
    echo   scripts\clai.bat status    check if healthy
    echo   scripts\clai.bat logs -f   watch what it is doing
    echo   scripts\clai.bat restart   restart it
    echo   scripts\clai.bat stop      stop it
    echo.
    exit /b 0
)
echo [warn] Background setup failed — starting in the foreground instead.
echo.

:run_foreground
echo.
echo   Ready!
echo   Dashboard: http://localhost:%WEB_PORT%
echo.

if "%HEADLESS%"=="1" (
    echo [ info] Headless mode enabled. Browser auto-open skipped.
) else (
    start "" "http://localhost:%WEB_PORT%"
)

venv\Scripts\python telegram_bot.py
pause
