param(
    [string]$Version = "0.1.0"
)

$ErrorActionPreference = "Stop"

$wsl = Get-Command wsl.exe -ErrorAction SilentlyContinue
if (-not $wsl) {
    throw "WSL is required to build .deb on Windows. Install WSL, then rerun this script."
}

$distrosRaw = & wsl.exe --list --quiet 2>$null
$distros = @($distrosRaw | ForEach-Object { ($_ -replace "`0", "").Trim() } | Where-Object { $_ })
if ($LASTEXITCODE -ne 0 -or $distros.Count -eq 0) {
    throw "WSL is installed but no Linux distribution is configured. Run: wsl --install -d Ubuntu"
}

$distro = $distros[0]

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$repoRootNormalized = $repoRoot -replace '\\', '/'
$repoRootWsl = (& wsl.exe -d $distro -- wslpath -a "$repoRootNormalized" 2>$null).Trim()

if ($LASTEXITCODE -ne 0 -or -not $repoRootWsl) {
    throw "Failed to resolve repository path in WSL."
}

$escapedVersion = $Version.Replace("'", "''")
$cmd = "cd '$repoRootWsl' && chmod +x src/scripts/build.deb.sh && ./src/scripts/build.deb.sh '$escapedVersion'"

Write-Host "[info] Running in WSL distro '$distro': $cmd"
wsl.exe -d $distro -- bash -lc "$cmd"

if ($LASTEXITCODE -ne 0) {
    throw "Debian package build failed in WSL."
}

Write-Host "[ok] Debian package build completed. Check dist/deb in the repository."
