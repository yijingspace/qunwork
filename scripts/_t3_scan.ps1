# -*- coding: utf-8 -*-
# Scan workspace for weekly-report related files (t3)
$ErrorActionPreference = 'SilentlyContinue'
$root = 'E:\QunWork\QunWork'
Get-ChildItem -Path $root -Recurse -File -Include *.md,*.py,*.toml,*.txt |
  Where-Object { $_.FullName -notmatch 'node_modules|\.git\\|egg-info|\\tests\\|\.state\\|\.qunwork\\|\.coworker\\' } |
  Select-String -Pattern '周报|weekly|week_stats|weekly_history|week-report|周报模板' -List |
  ForEach-Object { $_.Path } | Sort-Object -Unique | Select-Object -First 80
