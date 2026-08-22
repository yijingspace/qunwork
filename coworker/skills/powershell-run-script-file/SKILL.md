---
name: powershell-run-script-file
description: Windows 上通过 run_shell 执行 PowerShell 时，内联 -Command 的中文与 $ 变量常被外层 cmd 转义破坏（报 ParserError）。规则：把脚本写入 .ps1 文件，再运行 powershell -NoProfile -ExecutionPolicy Bypass -File script.ps1。附带中文标点正则 [，。、：；？！""''…—] 用于统计汉字数（不含标点）。
---

# PowerShell 脚本文件执行法（Windows）

## 问题
`run_shell` 在 Windows 上走 cmd，内联 `powershell -Command "..."` 中的 `$` 变量与中文标点常被转义破坏，报 `ParserError: 必须在"+"运算符后面提供一个值表达式`。

## 做法
1. 用 write_file 写入脚本（如 count_hanzi.ps1），脚本内保持 UTF-8 中文。
2. 执行：`powershell -NoProfile -ExecutionPolicy Bypass -File count_hanzi.ps1`

## 汉字计数（不含标点）
```powershell
$s = '目标字符串'
$n = ($s -replace '[，。、：；？！""''…—]', '').Length
Write-Output $n
```
注意：中文引号在单引号字符串内需按原文直接写入；正则字符类中的 `'` 在 PowerShell 单引号字符串里写作 `''` 转义。

## 适用场景
- 统计中文文案字数（"约 N 个汉字"校验）
- 任何含中文/特殊字符的 PowerShell 逻辑

