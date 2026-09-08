---
name: powershell-run-script-file
description: 在 Windows 上用 run_shell 跑 PowerShell。run_shell 是一个常驻的 pwsh/PowerShell REPL（不是 cmd），工具会自动把容易被内联解析破坏的命令（含中文、双引号、多行、$() 展开、以及你不小心用 `powershell -Command "..."` 自我嵌套的写法）改走 UTF-8 .ps1 脚本文件安全执行。因此直接发**裸 PowerShell 命令**即可，无需手写 .ps1 包装、无需再套一层 powershell -Command、不要用 Unix(bash) 命令。
---

# PowerShell 执行（Windows）——信任工具的自动路由

## 事实（纠正旧版误区）
- `run_shell` 在 Windows 上是**常驻 PowerShell 7 (pwsh) REPL**（无 pwsh 时回落 powershell.exe），**不是 cmd**，且已强制 UTF-8 代码页。
- 工具层已内置两道兜底，模型**不需要**自己绕：
  1. **内联脆弱自动转文件**：命令含中文 / 双引号 / 反引号 / 换行 / `$()` 时，`run_shell` 会把它写进 UTF-8-BOM 的 `.ps1` 再以 `& '路径'` 执行，一次成型、干净单行。
  2. **自我嵌套自动拆包**：若你写成 `powershell -NoProfile -Command "…"`（套了一层子进程 + cmd 式 `\"` 转义），工具会**剥离外层、只跑内层脚本**，避免被第二个 shell 再解析一次而拆成后台作业。

## 正确做法
- **直接写裸 PowerShell 命令**，例如：
  ```
  Write-Output "批次OK 时间 $(Get-Date -Format 'HH:mm:ss')"
  ```
  工具会安全处理引号与中文，拿到干净单行输出。**不要**自己再包 `powershell -Command "…"`，**不要**为简单一行命令手写 `.ps1` 辅助文件，**不要**用 `grep`/`wc`/`sed`/`find` 等 Unix 命令（这里是 PowerShell）。
- 只有当逻辑确实复杂（几十行的程序、多段脚本块）时，才用 `write_file` 写一个 `.ps1` 再 `& '路径'` —— 但简单取证/一行输出绝不需要。

## 关于"统计字数"
- 需要字符/汉字计数时用 **`text_stats` 工具**（唯一口径），不要自己写 PowerShell/正则去数，也不要"跑脚本数完再回填到正文"——回填会改变字数、导致数字永远不自洽的死循环。**若某个校验因编码反复失败，直接跳过它、照常交付正文。**
