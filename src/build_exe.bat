@echo off
setlocal

cd /d "%~dp0"

if "%~1"=="" (
    powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\build_windows_exe.ps1"
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\build_windows_exe.ps1" -Version "%~1"
)

if %ERRORLEVEL% neq 0 (
    echo.
    echo [FAIL] EXE build failed.
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo [OK] EXE build finished.
pause
exit /b 0
