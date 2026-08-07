# install.ps1 — graceful reinstall helper for QunWork desktop.
#
# WHY THIS EXISTS: overwriting the app while it is running can leave the sidecar
# half-replaced, and a hard-kill during install made the UI look like all data
# was wiped (it wasn't — data lives in %APPDATA%\Roaming\coworker, untouched by
# installs). This script:
#   1. Asks the running desktop app to quit cleanly (taskkill is a fallback).
#   2. Waits until every app/sidecar process is gone (files unlocked).
#   3. Installs the NSIS bundle silently.
#   4. Restarts the app.
#
# Usage:
#   powershell -File packaging\install.ps1          # uses the latest built installer
#   powershell -File packaging\install.ps1 -Path <path-to-setup.exe>

param(
    [string]$Path = ""
)

$ErrorActionPreference = "Stop"
$exeNames = @("openworker-desktop.exe", "qunwork-server.exe")

function Stop-App {
    # Graceful first: desktop apps usually honor WM_CLOSE; give it a moment,
    # then force-kill whatever is left so the installer never hits locked files.
    foreach ($name in $exeNames) {
        Get-Process -Name ($name -replace "\.exe$", "") -ErrorAction SilentlyContinue |
            Close-MainWindow -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 800
    foreach ($name in $exeNames) {
        Stop-Process -Name ($name -replace "\.exe$", "") -Force -ErrorAction SilentlyContinue
    }
    # Wait until both are gone (files released).
    for ($i = 0; $i -lt 20; $i++) {
        $left = Get-Process -Name ($exeNames | ForEach-Object { $_ -replace "\.exe$", "" }) -ErrorAction SilentlyContinue
        if (-not $left) { break }
        Start-Sleep -Milliseconds 300
    }
}

if (-not $Path) {
    $bundle = Join-Path $PSScriptRoot "..\surfaces\gui\src-tauri\target\release\bundle\nsis"
    $candidates = Get-ChildItem (Join-Path $bundle "*.exe") -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending
    if (-not $candidates) { throw "No installer found under $bundle — build first." }
    $Path = $candidates[0].FullName
}

Write-Host "Closing QunWork (graceful, then force)…"
Stop-App

Write-Host "Installing $Path (silent)…"
$installer = Start-Process -FilePath $Path -ArgumentList "/S" -Wait -PassThru
if ($installer.ExitCode -ne 0) {
    throw "Installer exited with code $($installer.ExitCode)"
}

Start-Sleep -Seconds 3
$appDir = Join-Path $env:LOCALAPPDATA "QunWork"
$app = Join-Path $appDir "openworker-desktop.exe"
if (Test-Path $app) {
    Write-Host "Starting QunWork…"
    Start-Process -FilePath $app
} else {
    Write-Host "Installed, but app not found at $app — start it manually."
}
Write-Host "Done."
