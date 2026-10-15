@echo off
setlocal enabledelayedexpansion

:: clai.bat — Control and inspect a background Clai TALOS install on Windows.
::
:: Usage:
::   clai status          Service state + dashboard health
::   clai logs [-f] [N]   Show output (-f to follow)
::   clai start           Start the service
::   clai stop            Stop the service
::   clai restart         Restart the service
::   clai enable          Register the background service
::   clai disable         Remove the background service
::   clai url             Print the dashboard URL
::   clai help            Show this help

set "TASK_NAME=ClaiTALOS"
set "INSTALL_DIR=%~dp0.."
pushd "%INSTALL_DIR%"
set "INSTALL_DIR=%CD%"
popd

set "SRC_DIR=%INSTALL_DIR%\src"
set "LOG_FILE=%SRC_DIR%\logs\talos.log"
set "WEB_PORT=8080"

:: Read WEB_PORT from .env if it exists
if exist "%SRC_DIR%\.env" (
    for /f "usebackq tokens=1,* delims==" %%A in ("%SRC_DIR%\.env") do (
        if /I "%%A"=="WEB_PORT" (
            set "WEB_PORT=%%B"
            set "WEB_PORT=!WEB_PORT: =!"
        )
    )
)

set "CMD=%~1"
if "%CMD%"=="" set "CMD=status"

if /I "%CMD%"=="status"  goto :cmd_status
if /I "%CMD%"=="logs"    goto :cmd_logs
if /I "%CMD%"=="start"   goto :cmd_start
if /I "%CMD%"=="stop"    goto :cmd_stop
if /I "%CMD%"=="restart" goto :cmd_restart
if /I "%CMD%"=="enable"  goto :cmd_enable
if /I "%CMD%"=="disable" goto :cmd_disable
if /I "%CMD%"=="url"     goto :cmd_url
if /I "%CMD%"=="help"    goto :cmd_help
if /I "%CMD%"=="/?"      goto :cmd_help
if /I "%CMD%"=="-h"      goto :cmd_help

echo [FAIL] Unknown command: %CMD%
call :cmd_help
exit /b 1

:: ── status ────────────────────────────────────────────────────────────

:cmd_status
echo.
echo   Clai TALOS
echo.

:: Check if task exists
schtasks /query /tn "%TASK_NAME%" >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [  X] Service not installed
    echo [info] Run: scripts\install_service.bat /install
    echo.
    exit /b 1
)

:: Get status line
set "RUNNING=0"
for /f "tokens=*" %%L in ('schtasks /query /tn "%TASK_NAME%" /fo list /v ^| findstr /i "Status:"') do (
    echo %%L | findstr /i "Running" >nul && set "RUNNING=1"
)
if "%RUNNING%"=="1" (
    echo [ OK ] Service is running
) else (
    echo [  X ] Service is not running
)

:: Check dashboard
powershell -Command "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:%WEB_PORT%/' -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop; exit 0 } catch { exit 1 }" >nul 2>nul
if %ERRORLEVEL% equ 0 (
    echo [ OK ] Dashboard responding on port %WEB_PORT%
) else (
    echo [  X ] Dashboard not responding on port %WEB_PORT%
)

echo.
echo   Dashboard: http://localhost:%WEB_PORT%
echo.
echo   clai logs -f    follow output      clai restart   restart service
echo   clai start      start it           clai stop      stop it
echo.
exit /b 0

:: ── logs ──────────────────────────────────────────────────────────────

:cmd_logs
set "FOLLOW=0"
set "LINES=50"
shift

:logs_parse
if "%~1"=="" goto :logs_run
if /I "%~1"=="-f" set "FOLLOW=1"
if /I "%~1"=="--follow" set "FOLLOW=1"
echo %~1 | findstr /r "^[0-9][0-9]*$" >nul && set "LINES=%~1"
shift
goto :logs_parse

:logs_run
if not exist "%LOG_FILE%" (
    echo [info] No log file yet at %LOG_FILE%
    exit /b 0
)

if "%FOLLOW%"=="1" (
    powershell -Command "Get-Content '%LOG_FILE%' -Tail %LINES% -Wait"
) else (
    powershell -Command "Get-Content '%LOG_FILE%' -Tail %LINES%"
)
exit /b 0

:: ── start ─────────────────────────────────────────────────────────────

:cmd_start
schtasks /query /tn "%TASK_NAME%" >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [FAIL] Service not installed. Run: clai enable
    exit /b 1
)

:: Check if already running
set "RUNNING=0"
for /f "tokens=*" %%L in ('schtasks /query /tn "%TASK_NAME%" /fo list /v ^| findstr /i "Status:"') do (
    echo %%L | findstr /i "Running" >nul && set "RUNNING=1"
)
if "%RUNNING%"=="1" (
    echo [info] Already running.
    exit /b 0
)

schtasks /run /tn "%TASK_NAME%" >nul 2>nul
if %ERRORLEVEL% equ 0 (
    echo [  OK] Started.
) else (
    echo [FAIL] Could not start.
    exit /b 1
)
exit /b 0

:: ── stop ──────────────────────────────────────────────────────────────

:cmd_stop
schtasks /query /tn "%TASK_NAME%" >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [info] Service not installed.
    exit /b 0
)

schtasks /end /tn "%TASK_NAME%" >nul 2>nul
taskkill /f /im "service_runner.bat" >nul 2>nul
echo [  OK] Stopped.
exit /b 0

:: ── restart ───────────────────────────────────────────────────────────

:cmd_restart
call :cmd_stop
timeout /t 2 /nobreak >nul
call :cmd_start
exit /b 0

:: ── enable ────────────────────────────────────────────────────────────

:cmd_enable
call "%INSTALL_DIR%\scripts\install_service.bat" /install
exit /b %ERRORLEVEL%

:: ── disable ───────────────────────────────────────────────────────────

:cmd_disable
call "%INSTALL_DIR%\scripts\install_service.bat" /delete
exit /b %ERRORLEVEL%

:: ── url ───────────────────────────────────────────────────────────────

:cmd_url
echo http://localhost:%WEB_PORT%
exit /b 0

:: ── help ──────────────────────────────────────────────────────────────

:cmd_help
echo.
echo   clai — control your Clai TALOS assistant
echo.
echo     clai status          service and dashboard state
echo     clai logs [-f] [N]   show output (-f to follow)
echo     clai start           start it
echo     clai stop            stop it
echo     clai restart         restart it
echo     clai enable          register background service (starts at logon)
echo     clai disable         remove background service
echo     clai url             print the dashboard URL
echo.
exit /b 0
