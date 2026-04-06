param(
    [string]$Version = "0.1.0"
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $scriptDir "..\src\scripts\build_deb.ps1"

& $target -Version $Version
exit $LASTEXITCODE
