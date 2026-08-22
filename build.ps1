# 设置环境变量
$env:LIBCLANG_PATH = "C:\Python314\Lib\site-packages\clang\native"
$env:NSISDIR = "C:\Program Files (x86)\NSIS"

# 进入Tauri项目目录
Set-Location "E:\QunWork\QunWork\surfaces\gui"

Write-Host "=== 开始构建 ===" -ForegroundColor Cyan

# 构建应用
Write-Host "1. 构建前端和Tauri应用..." -ForegroundColor Yellow
& cargo tauri build --bundles nsis 2>&1 | Write-Host

Write-Host "
=== 构建完成 ===" -ForegroundColor Green
