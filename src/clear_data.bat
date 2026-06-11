@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

set "LOCAL_DATA_DIR=%LOCALAPPDATA%\Clai_TALOS"
if not defined LOCALAPPDATA set "LOCAL_DATA_DIR=%USERPROFILE%\AppData\Local\Clai_TALOS"
set "OVERRIDE_DATA_DIR=%TALOS_DATA_DIR%"
if defined OVERRIDE_DATA_DIR set "OVERRIDE_DATA_DIR=%OVERRIDE_DATA_DIR:"=%"

echo Clearing all Clai_TALOS user data...

call :clear_root "%SCRIPT_DIR%" "workspace"

if defined OVERRIDE_DATA_DIR (
    if /I not "%OVERRIDE_DATA_DIR%"=="%SCRIPT_DIR%" (
        call :clear_root "%OVERRIDE_DATA_DIR%" "TALOS_DATA_DIR"
    )
)

if /I not "%LOCAL_DATA_DIR%"=="%SCRIPT_DIR%" (
    if /I not "%LOCAL_DATA_DIR%"=="%OVERRIDE_DATA_DIR%" (
        call :clear_root "%LOCAL_DATA_DIR%" "LOCALAPPDATA"
    )
)

echo Done. All user data cleared.
echo Run onboarding again on next startup.
exit /b 0

:clear_root
set "ROOT=%~1"
set "LABEL=%~2"

if "%ROOT%"=="" goto :eof

if not exist "%ROOT%" (
    echo [skip] %LABEL% path not found: %ROOT%
    goto :eof
)

echo [info] Clearing data at %ROOT%

call :del_file "%ROOT%\talos.db"
call :del_file "%ROOT%\talos.db-wal"
call :del_file "%ROOT%\talos.db-shm"
call :del_file "%ROOT%\.env"
call :del_file "%ROOT%\.credentials"
call :del_file "%ROOT%\.google_oauth.json"
call :del_file "%ROOT%\.security.log"
call :del_file "%ROOT%\.setup_config"
call :del_file "%ROOT%\.tools_config"
call :del_file "%ROOT%\terminal_config.json"

call :del_dir "%ROOT%\.himalaya"
call :del_dir "%ROOT%\.browser-profile"
call :del_dir "%ROOT%\projects"
call :del_dir "%ROOT%\logs"

if /I not "%ROOT%"=="%SCRIPT_DIR%" (
    call :del_dir "%ROOT%\bin"
)

goto :eof

:del_file
if exist "%~1" del /f /q "%~1" >nul 2>nul
goto :eof

:del_dir
if exist "%~1" rmdir /s /q "%~1" >nul 2>nul
goto :eof