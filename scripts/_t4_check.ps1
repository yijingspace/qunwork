$ErrorActionPreference = "Stop"
Set-Location "E:\QunWork\QunWork"
$w31 = "weekly_reports\2026-W31_周报.md"
$w32 = "weekly_reports\2026-W32_周报.md"
Write-Output ("W31 exists: " + (Test-Path $w31))
Write-Output ("W32 exists: " + (Test-Path $w32))
if (Test-Path $w31) {
  Write-Output ("W31 SHA256: " + (Get-FileHash $w31 -Algorithm SHA256).Hash)
  Write-Output ("W31 length: " + (Get-Item $w31).Length)
}
if (Test-Path $w32) {
  Write-Output ("W32 SHA256: " + (Get-FileHash $w32 -Algorithm SHA256).Hash)
  Write-Output ("W32 length: " + (Get-Item $w32).Length)
}
