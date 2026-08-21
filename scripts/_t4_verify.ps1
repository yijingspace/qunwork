$ErrorActionPreference = "Stop"
Set-Location "E:\QunWork\QunWork"
Write-Output "=== commit count (2026-07-27..08-03) ==="
$n = (git log --since="2026-07-27 00:00" --until="2026-08-03 00:00" --pretty=format:"%h" | Measure-Object -Line).Lines
Write-Output ("count=" + $n)
Write-Output "=== daily distribution ==="
git log --since="2026-07-27 00:00" --until="2026-08-03 00:00" --pretty=format:"%ad" --date=format:"%Y-%m-%d" | Group-Object | Sort-Object Name | ForEach-Object { Write-Output ($_.Name + " : " + $_.Count) }
Write-Output "=== net diff 56b3c62..ed9297e ==="
git diff --shortstat 56b3c62 ed9297e
Write-Output "=== per-commit numstat summation (same window) ==="
$lines = git log --since="2026-07-27 00:00" --until="2026-08-03 00:00" --numstat --pretty=format:""
$ins = 0; $dels = 0; $files = 0
foreach ($l in $lines) {
  if ($l -match "^\d+\s+\d+\s+") {
    $parts = $l -split "\s+"
    $ins += [int]$parts[0]; $dels += [int]$parts[1]; $files++
  }
}
Write-Output ("summation ins=" + $ins + " dels=" + $dels + " file-entries=" + $files)
Write-Output "=== weekly_reports dir ==="
Get-ChildItem "weekly_reports" | Select-Object Name, Length | Format-Table -AutoSize | Out-String | Write-Output
Write-Output "=== swarm_history.py exists? ==="
Test-Path "scripts\swarm_history.py"
