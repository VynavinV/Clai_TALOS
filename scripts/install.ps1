#Requires -Version 5.1
<#
.SYNOPSIS
    Cross-platform installer for Clai TALOS on Windows.

.DESCRIPTION
    Downloads and installs Clai TALOS from GitHub Releases.

.PARAMETER Version
    Version to install (default: latest release).

.PARAMETER InstallDir
    Installation directory (default: $env:LOCALAPPDATA\Clai_TALOS).

.PARAMETER Uninstall
    Remove Clai TALOS.

.EXAMPLE
    irm https://raw.githubusercontent.com/VynavinV/Clai_TALOS/master/scripts/install.ps1 | iex
.EXAMPLE
    .\install.ps1 -Version "0.1.0"
.EXAMPLE
    .\install.ps1 -Uninstall
#>

param(
    [string]$Version = "",
    [string]$InstallDir = "",
    [switch]$Uninstall,
    [switch]$Help
)

$ErrorActionPreference = "Stop"
$Repo = "VynavinV/Clai_TALOS"
$BaseUrl = "https://github.com/$Repo/releases"

if ($Help) {
    Write-Host @"
Usage: install.ps1 [OPTIONS]

Install Clai TALOS on Windows.

Options:
  -Version <ver>    Version to install (default: latest release)
  -InstallDir <dir> Installation directory (default: `$env:LOCALAPPDATA\Clai_TALOS)
  -Uninstall        Remove Clai TALOS
  -Help             Show this help

Examples:
  irm https://raw.githubusercontent.com/$Repo/master/scripts/install.ps1 | iex
  .\install.ps1 -Version "0.1.0"
  .\install.ps1 -Uninstall
"@
    exit 0
}

# --- Uninstall ---
if ($Uninstall) {
    if ([string]::IsNullOrEmpty($InstallDir)) {
        $InstallDir = Join-Path $env:LOCALAPPDATA "Clai_TALOS"
    }
    Write-Host "[info] Uninstalling Clai TALOS from $InstallDir ..."

    # Remove from PATH
    $currentPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $binDir = Split-Path $InstallDir -Parent
    if ($currentPath -and $currentPath -split ";" -contains $binDir) {
        $newPath = ($currentPath -split ";" | Where-Object { $_ -ne $binDir }) -join ";"
        [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
        Write-Host "[info] Removed $binDir from user PATH."
    }

    Remove-Item -Recurse -Force $InstallDir -ErrorAction SilentlyContinue
    Write-Host "[ok] Clai TALOS uninstalled."
    exit 0
}

# --- Detect arch ---
$arch = if ([Environment]::Is64BitOperatingSystem) { "x64" } else { "x86" }

# --- Determine install dir ---
if ([string]::IsNullOrEmpty($InstallDir)) {
    $InstallDir = Join-Path $env:LOCALAPPDATA "Clai_TALOS"
}

# --- Get latest version ---
if ([string]::IsNullOrEmpty($Version)) {
    Write-Host "[info] Fetching latest release..."
    try {
        $response = Invoke-RestMethod -Uri "$BaseUrl/latest" -MaximumRedirection 0 -ErrorAction SilentlyContinue
        $Version = $response.tag_name -replace '^v', ''
    } catch {
        # Fallback: follow redirect manually
        try {
            $resp = Invoke-WebRequest -Uri "$BaseUrl/latest" -MaximumRedirection 5 -UseBasicParsing
            $Version = ($resp.BaseResponse.ResponseUri.AbsolutePath -split '/')[-1] -replace '^v', ''
        } catch {
            # Last resort: use the releases API
            $releases = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest"
            $Version = $releases.tag_name -replace '^v', ''
        }
    }
    if ([string]::IsNullOrEmpty($Version)) {
        Write-Host "[fail] Could not determine latest version. Use -Version to specify." -ForegroundColor Red
        exit 1
    }
    Write-Host "[info] Latest version: $Version"
} else {
    $Version = $Version -replace '^v', ''
}

$AssetName = "ClaiTALOS-windows-$arch.zip"
$DownloadUrl = "$BaseUrl/download/v$Version/$AssetName"

Write-Host ""
Write-Host "[info] Clai TALOS Installer" -ForegroundColor Cyan
Write-Host "[info] OS:        Windows"
Write-Host "[info] Arch:      $arch"
Write-Host "[info] Version:   $Version"
Write-Host "[info] Install to: $InstallDir"
Write-Host ""

# --- Download ---
$zipPath = Join-Path $env:TEMP "clai-talos-install-$AssetName"

Write-Host "[info] Downloading: $AssetName"
Write-Host "[info] From: $DownloadUrl"
Write-Host ""

try {
    # Use BITS if available (better progress), fall back to WebClient
    if (Get-Command Start-BitsTransfer -ErrorAction SilentlyContinue) {
        Start-BitsTransfer -Source $DownloadUrl -Destination $zipPath -DisplayName "Clai TALOS"
    } else {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $webClient = New-Object System.Net.WebClient
        $webClient.DownloadFile($DownloadUrl, $zipPath)
    }
} catch {
    Write-Host "[fail] Download failed: $_" -ForegroundColor Red
    exit 1
}

$sizeMB = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
Write-Host ""
Write-Host "[ok] Downloaded ${sizeMB}MB"

# --- Extract ---
Write-Host "[info] Extracting..."

$stagingDir = Join-Path $env:TEMP "clai-talos-install-staging"
if (Test-Path $stagingDir) { Remove-Item -Recurse -Force $stagingDir }
New-Item -ItemType Directory -Force -Path $stagingDir | Out-Null

Expand-Archive -Path $zipPath -DestinationPath $stagingDir -Force
Remove-Item $zipPath -Force

# --- Install ---
if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir }
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

# Find the extracted content
$extractedContent = Get-ChildItem $stagingDir -Directory | Select-Object -First 1
if ($extractedContent) {
    Copy-Item -Path (Join-Path $extractedContent.FullName "*") -Destination $InstallDir -Recurse -Force
} else {
    Copy-Item -Path (Join-Path $stagingDir "*") -Destination $InstallDir -Recurse -Force
}

Remove-Item -Recurse -Force $stagingDir

# Find the executable
$exePath = Get-ChildItem -Path $InstallDir -Filter "ClaiTALOS.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $exePath) {
    $exePath = Get-ChildItem -Path $InstallDir -Filter "*.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
}

if (-not $exePath) {
    Write-Host "[fail] Could not find executable after extraction." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "[ok] Installed to $InstallDir"
Write-Host "[ok] Executable: $($exePath.FullName)"

# --- Add to PATH ---
$binDir = Split-Path $exePath.FullName -Parent
$currentPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($currentPath -and $currentPath -split ";" -contains $binDir) {
    Write-Host "[info] Already in PATH."
} else {
    $newPath = if ($currentPath) { "$currentPath;$binDir" } else { $binDir }
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Write-Host "[ok] Added $binDir to user PATH."
    Write-Host "[info] Restart your terminal for PATH changes to take effect."
}

# --- Create desktop shortcut ---
$desktopPath = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktopPath "Clai TALOS.lnk"

try {
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $exePath.FullName
    $shortcut.WorkingDirectory = (Split-Path $exePath.FullName -Parent)
    $shortcut.Description = "Clai TALOS AI Assistant"
    $shortcut.Save()
    Write-Host "[ok] Desktop shortcut created."
} catch {
    Write-Host "[warn] Could not create desktop shortcut: $_" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=========================================" -ForegroundColor Green
Write-Host "  Clai TALOS v$Version installed!" -ForegroundColor Green
Write-Host "=========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Run it with:  ClaiTALOS"
Write-Host "Dashboard:    http://localhost:8080"
Write-Host ""
Write-Host "Uninstall:    .\install.ps1 -Uninstall"
Write-Host ""
