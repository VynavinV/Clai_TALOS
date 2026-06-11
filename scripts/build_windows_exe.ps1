Param(
    [string]$PythonVersion = "3.12",
    [string]$Version = ""
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $scriptDir "..\src\scripts\build_windows_exe.ps1"

& $target -PythonVersion $PythonVersion -Version $Version
exit $LASTEXITCODE
