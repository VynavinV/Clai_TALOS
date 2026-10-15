@echo off
setlocal enabledelayedexpansion

:: install_service.bat — Manage Clai TALOS background service via Task Scheduler
::
:: Usage:
::   install_service.bat /install   Register the scheduled task (starts at logon)
::   install_service.bat /delete    Remove the scheduled task
::   install_service.bat /start     Start the task now
::   install_service.bat /stop      Stop the running task
::   install_service.bat /status    Print whether the task exists and is running

set "TASK_NAME=ClaiTALOS"
set "INSTALL_DIR=%~dp0.."
pushd "%INSTALL_DIR%"
set "INSTALL_DIR=%CD%"
popd

set "SRC_DIR=%INSTALL_DIR%\src"
set "RUNNER=%INSTALL_DIR%\scripts\service_runner.bat"

if /I "%~1"=="/install" goto :do_install
if /I "%~1"=="/delete" goto :do_delete
if /I "%~1"=="/start"  goto :do_start
if /I "%~1"=="/stop"   goto :do_stop
if /I "%~1"=="/status" goto :do_status

echo Usage:
echo   install_service.bat /install   Register background service (starts at logon)
echo   install_service.bat /delete    Remove background service
echo   install_service.bat /start     Start it now
echo   install_service.bat /stop      Stop it
echo   install_service.bat /status    Check if running
exit /b 1

:: ── /install ──────────────────────────────────────────────────────────

:do_install
:: Write the service runner (restart loop)
> "%RUNNER%" echo @echo off
>> "%RUNNER%" echo :: Clai TALOS service runner — auto-generated, restarts on crash
>> "%RUNNER%" echo cd /d "%SRC_DIR%"
>> "%RUNNER%" echo set "PYTHONUNBUFFERED=1"
>> "%RUNNER%" echo :loop
>> "%RUNNER%" echo echo [%%date%% %%time%%] Starting Clai TALOS...
>> "%RUNNER%" echo call venv\Scripts\python telegram_bot.py
>> "%RUNNER%" echo echo [%%date%% %%time%%] Process exited with code %%ERRORLEVEL%%, restarting in 5s...
>> "%RUNNER%" echo timeout /t 5 /nobreak ^>nul
>> "%RUNNER%" echo goto loop

echo [info] Registering scheduled task '%TASK_NAME%'...

:: Remove old task if it exists
schtasks /query /tn "%TASK_NAME%" >nul 2>nul
if %ERRORLEVEL% equ 0 schtasks /delete /tn "%TASK_NAME%" /f >nul 2>nul

:: Create the task
schtasks /create /tn "%TASK_NAME%" /tr "\"%RUNNER%\"" /sc onlogon /rl limited /f >nul 2>nul

if %ERRORLEVEL% neq 0 (
    echo [FAIL] Could not create scheduled task.
    echo [info] Try running as Administrator if you get access denied.
    exit /b 1
)

echo [  OK] Scheduled task '%TASK_NAME%' registered.
echo [info] Starts automatically at logon.
echo [info] Use: scripts\clai.bat status

:: Start it now
call :do_start
exit /b 0

:: ── /delete ───────────────────────────────────────────────────────────

:do_delete
schtasks /query /tn "%TASK_NAME%" >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [info] Task '%TASK_NAME%' is not installed.
    exit /b 0
)

:: Stop first
schtasks /end /tn "%TASK_NAME%" >nul 2>nul 2>nul
timeout /t 2 /nobreak >nul

:: Kill any leftover runner/python processes from our venv
taskkill /f /im "service_runner.bat" >nul 2>nul

schtasks /delete /tn "%TASK_NAME%" /f >nul 2>nul
if %ERRORLEVEL% equ 0 (
    echo [  OK] Scheduled task '%TASK_NAME%' removed.
) else (
    echo [FAIL] Could not remove task. Try running as Administrator.
    exit /b 1
)

:: Clean up the runner
if exist "%RUNNER%" del /f /q "%RUNNER%"
exit /b 0

:: ── /start ────────────────────────────────────────────────────────────

:do_start
schtasks /query /tn "%TASK_NAME%" >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [FAIL] Task '%TASK_NAME%' is not installed. Run: install_service.bat /install
    exit /b 1
)

schtasks /run /tn "%TASK_NAME%" >nul 2>nul
if %ERRORLEVEL% equ 0 (
    echo [  OK] Task '%TASK_NAME%' started.
) else (
    echo [FAIL] Could not start task.
    exit /b 1
)
exit /b 0

:: ── /stop ─────────────────────────────────────────────────────────────

:do_stop
schtasks /query /tn "%TASK_NAME%" >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [info] Task '%TASK_NAME%' is not installed.
    exit /b 0
)

schtasks /end /tn "%TASK_NAME%" >nul 2>nul

:: Also kill the runner and its python child
taskkill /f /im "service_runner.bat" >nul 2>nul
taskkill /f /fi "WINDOWTITLE eq ClaiTALOS*" >nul 2>nul

echo [  OK] Task '%TASK_NAME%' stopped.
exit /b 0

:: ── /status ───────────────────────────────────────────────────────────

:do_status
schtasks /query /tn "%TASK_NAME%" >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [info] Task '%TASK_NAME%' is not installed.
    exit /b 1
)

:: Check running state
set "TASK_STATUS="
for /f "tokens=*" %%L in ('schtasks /query /tn "%TASK_NAME%" /fo list /v ^| findstr /i "Status:"') do (
    set "TASK_STATUS=%%L"
)
echo [info] Task:   %TASK_NAME%
echo [info] %TASK_STATUS%
exit /b 0
