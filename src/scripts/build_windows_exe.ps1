Param(
    [string]$PythonVersion = "3.12",
    [string]$Version = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

Write-Host "[build] Preparing Windows EXE build in $Root"

function Get-VersionLabel {
    param([string]$ProvidedVersion)

    if ($ProvidedVersion -and $ProvidedVersion.Trim().Length -gt 0) {
        return $ProvidedVersion.Trim()
    }

    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $sha = "nogit"
    $dirty = ""

    if (Get-Command git -ErrorAction SilentlyContinue) {
        try {
            $shaOutput = git rev-parse --short HEAD 2>$null
            if ($LASTEXITCODE -eq 0 -and $shaOutput) {
                $sha = $shaOutput.Trim()
            }

            git diff --quiet --ignore-submodules HEAD 2>$null
            if ($LASTEXITCODE -ne 0) {
                $dirty = "-dirty"
            }
        } catch {
            $sha = "nogit"
        }
    }

    return "$timestamp-$sha$dirty"
}

if (Get-Command py -ErrorAction SilentlyContinue) {
    $pyCmd = "py -$PythonVersion"
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $pyCmd = "python"
} else {
    throw "Python launcher not found. Install Python 3.11+ first."
}

$venvDir = ".venv"
if (-not (Test-Path $venvDir)) {
    $venvDir = "venv"
}

if (-not (Test-Path $venvDir)) {
    $venvDir = "venv"
    Write-Host "[build] Creating venv at $venvDir"
    Invoke-Expression "$pyCmd -m venv $venvDir"
}

$pythonExe = Join-Path $Root "$venvDir\Scripts\python.exe"
$pipExe = Join-Path $Root "$venvDir\Scripts\pip.exe"

Write-Host "[build] Installing dependencies"
& $pythonExe -m pip install --upgrade pip
& $pipExe install -r requirements.txt pyinstaller

Write-Host "[build] Building EXE with PyInstaller"
& $pythonExe -m PyInstaller --noconfirm --clean talos_exe.spec

$distDir = Join-Path $Root "dist"
$outDir = Join-Path $distDir "ClaiTALOS"
if (-not (Test-Path $outDir)) {
    throw "Build output not found at $outDir"
}

$versionLabel = Get-VersionLabel -ProvidedVersion $Version
$safeVersion = ($versionLabel -replace "[^A-Za-z0-9._-]", "-")

$zipName = "ClaiTALOS-windows-x64-$safeVersion.zip"
$zipPath = Join-Path $distDir $zipName
if (Test-Path $zipPath) {
    Remove-Item $zipPath -Force
}

Write-Host "[build] Creating archive $zipPath"
Compress-Archive -Path (Join-Path $outDir "*") -DestinationPath $zipPath -CompressionLevel Optimal

$latestZipPath = Join-Path $distDir "ClaiTALOS-windows-x64-latest.zip"
if (Test-Path $latestZipPath) {
    Remove-Item $latestZipPath -Force
}
Copy-Item -Path $zipPath -Destination $latestZipPath

$hashFile = Join-Path $distDir "SHA256SUMS-$safeVersion.txt"
$latestHashFile = Join-Path $distDir "SHA256SUMS.txt"
$hash = Get-FileHash -Path $zipPath -Algorithm SHA256

$hashLineVersioned = "$($hash.Hash.ToLower())  $zipName"
$hashLineLatest = "$($hash.Hash.ToLower())  ClaiTALOS-windows-x64-latest.zip"

$hashLineVersioned | Set-Content -Path $hashFile -Encoding UTF8
@($hashLineVersioned, $hashLineLatest) | Set-Content -Path $latestHashFile -Encoding UTF8

$manifestPath = Join-Path $distDir "build-manifest.json"
$manifestEntry = [ordered]@{
    built_at_utc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    version = $safeVersion
    artifact = $zipName
    artifact_latest = "ClaiTALOS-windows-x64-latest.zip"
    sha256 = $hash.Hash.ToLower()
}

$manifest = @()
if (Test-Path $manifestPath) {
    try {
        $existing = Get-Content $manifestPath -Raw | ConvertFrom-Json
        if ($existing -is [System.Array]) {
            $manifest = @($existing)
        } elseif ($null -ne $existing) {
            $manifest = @($existing)
        }
    } catch {
        $manifest = @()
    }
}
$manifest = @($manifestEntry) + $manifest
$manifest | ConvertTo-Json -Depth 4 | Set-Content -Path $manifestPath -Encoding UTF8

Write-Host "[done] EXE artifact: $zipPath"
Write-Host "[done] Latest artifact alias: $latestZipPath"
Write-Host "[done] Checksums: $hashFile"
Write-Host "[done] Latest checksums: $latestHashFile"
Write-Host "[done] Build manifest: $manifestPath"
