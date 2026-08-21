$d1 = Get-Date '2026-08-03'
$d2 = Get-Date '2026-08-10'
$cal = [System.Globalization.CultureInfo]::InvariantCulture.Calendar
$week = $cal.GetWeekOfYear($d1, [System.Globalization.CalendarWeekRule]::FirstFourDayWeek, [System.DayOfWeek]::Monday)
Write-Output ('2026-08-03 weekday: ' + (Get-Date '2026-08-03' -Format 'dddd'))
Write-Output ('2026-08-10 weekday: ' + (Get-Date '2026-08-10' -Format 'dddd'))
Write-Output ('ISO week label: 2026-W' + $week.ToString('00'))
