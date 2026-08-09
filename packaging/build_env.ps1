# build_env.ps1 — one-command packager for QunWork desktop.
#
# WHY: `tauri build` on Windows needs three pieces of environment that vary per
# machine — libclang.dll (Rust bindgen), CMake (tauri build) and LLVM. Missing
# any of them fails with "unable to find libclang" or "cmake not found". This
# script probes them automatically, sets the env, then delegates to
# build_windows.ps1. One command, no manual path bookkeeping:
#
#   powershell -ExecutionPolicy Bypass -File packaging\build_env.ps1
#   powershell -ExecutionPolicy Bypass -File packaging\build_env.ps1 -Bundles nsis
#
# Probe order (first hit wins):
#   libclang: C:\Python*\Lib\site-packages\clang\native (any Python), .venv,
#             C:\Program Files\LLVM\bin, vcpkg, Chocolatey
#   cmake   : C:\Program Files\CMake\bin, C:\ProgramData\chocolatey\bin, scoop
#   llvm    : C:\Program Files\LLVM\bin, C:\Program Files\Microsoft Visual Studio\*\*\VC\Tools\Llvm
#
# Requires a UTF-8 BOM on this file (PowerShell 5.1 reads no-BOM files as ANSI).

param(
    [string[]]$Bundles = @(),
    [switch]$ProbeOnly
)

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Here

function Find-First([string[]]$Candidates) {
    foreach ($c in $Candidates) {
        if ($null -eq $c -or $c -eq "") { continue }
        if (Test-Path -LiteralPath $c) { return $c }
    }
    return $null
}

# --- probe libclang.dll ------------------------------------------------------
$libclang = $null
# 1) any system Python's clang package (most common source)
$pyCands = @()
Get-ChildItem "C:\Python*\Lib\site-packages\clang\native\libclang.dll" -ErrorAction SilentlyContinue |
    ForEach-Object { $pyCands += $_.FullName }
# 2) project venv
$pyCands += (Join-Path $Root ".venv\Lib\site-packages\clang\native\libclang.dll")
# 3) LLVM / vcpkg / chocolatey
$pyCands += "C:\Program Files\LLVM\bin\libclang.dll"
$pyCands += "$env:LOCALAPPDATA\Programs\Python\*\Lib\site-packages\clang\native\libclang.dll"
$libclang = Find-First $pyCands

# --- probe cmake -------------------------------------------------------------
$cmake = Find-First @(
    "C:\Program Files\CMake\bin",
    "C:\ProgramData\chocolatey\bin",
    "$env:USERPROFILE\scoop\shims"
)

# --- probe LLVM bin ----------------------------------------------------------
$llvm = Find-First @(
    "C:\Program Files\LLVM\bin",
    (Get-ChildItem "C:\Program Files\Microsoft Visual Studio\*\*\VC\Tools\Llvm" -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName)
)

# --- report + apply ----------------------------------------------------------
if (-not $libclang) {
    Write-Host "[build_env] WARNING: libclang.dll not found — Rust bindgen will fail." -ForegroundColor Yellow
    Write-Host "           Install it: pip install clang  (system Python), or install LLVM." -ForegroundColor Yellow
} else {
    Write-Host "[build_env] libclang.dll : $libclang" -ForegroundColor Green
    $env:LIBCLANG_PATH = Split-Path -Parent $libclang
}
if ($cmake) {
    Write-Host "[build_env] cmake dir    : $cmake" -ForegroundColor Green
    $env:PATH = "$cmake;$env:PATH"
} else {
    Write-Host "[build_env] WARNING: cmake not found — tauri build will fail." -ForegroundColor Yellow
}
if ($llvm) {
    Write-Host "[build_env] LLVM bin dir : $llvm" -ForegroundColor Green
    $env:PATH = "$llvm;$env:PATH"
} else {
    Write-Host "[build_env] WARNING: LLVM not found (optional — only needed by some crates)." -ForegroundColor Yellow
}

# --- delegate to the real packager -------------------------------------------
if ($ProbeOnly) {
    Write-Host "[build_env] Probe-only: environment OK, skipping build." -ForegroundColor Cyan
    exit 0
}
$buildScript = Join-Path $Here "build_windows.ps1"
if (-not (Test-Path $buildScript)) {
    throw "build_windows.ps1 not found next to build_env.ps1"
}
$argsList = @("-File", $buildScript)
if ($Bundles.Count -gt 0) {
    $argsList += @("-Bundles", ($Bundles -join ","))
}
Write-Host "[build_env] Running: powershell $($argsList -join ' ')" -ForegroundColor Cyan
& powershell -NoProfile -ExecutionPolicy Bypass @argsList
exit $LASTEXITCODE
