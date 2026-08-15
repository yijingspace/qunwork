"""P1-6 Skill 安全评分 — 静态分析导入 Skill 的风险。

分析 SKILL.md 中的 Python/scripts 内容, 按以下维度打风险分 (0-100):
  - shell 执行 (subprocess, os.system, exec)
  - 绝对路径读写 (open("/..."), pathlib.Path("/..."))
  - 网络请求 (requests, httpx, urllib)
  - 动态执行 (eval, exec, compile)

评分越高越危险:
  0-20:  低风险 (只读操作)
  21-50: 中风险 (有写操作但无网络)
  51-80: 高风险 (有 shell 或网络)
  81+:   极高风险 (有动态执行 + shell + 网络)
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any


# 风险模式 (pattern, weight, label)
_RISK_PATTERNS: list[tuple[str, int, str]] = [
    # -- 动态执行 (最高风险) --
    (r"\beval\s*\(", 30, "eval() 动态执行"),
    (r"\bexec\s*\(", 30, "exec() 动态执行"),
    (r"\bcompile\s*\(", 20, "compile() 动态编译"),
    (r"__import__\s*\(", 25, "__import__() 动态导入"),
    # -- Shell 执行 (POSIX + Windows) --
    (r"\bos\.system\s*\(", 25, "os.system() shell 执行"),
    (r"\bsubprocess\.(run|call|Popen|check_output|check_call)\s*\(", 20, "subprocess shell 执行"),
    (r"\bos\.popen\s*\(", 20, "os.popen() shell 执行"),
    (r"\bcommands\.(getoutput|getstatusoutput)\s*\(", 20, "commands 模块 shell 执行"),
    (r"(?i)powershell\s+(?:-Command|-c|-F)", 22, "PowerShell 子进程调用"),
    (r"(?i)cmd\.(exe)?\s*/[cCkK]", 22, "cmd.exe shell 执行"),
    (r"(?i)\bStart-Process\b", 18, "Start-Process 子进程启动"),
    (r"(?i)&\s*['\"][^\n]{3,}['\"]\s*['\"]?[A-Z]:[\\/]", 18, "& 调用符执行 Windows 路径脚本"),
    # -- 网络请求 --
    (r"\brequests\.(get|post|put|delete|patch|head)\s*\(", 15, "requests 网络请求"),
    (r"\bhttpx\.(get|post|put|delete|patch)\s*\(", 15, "httpx 网络请求"),
    (r"\burllib\.request\s*\(", 15, "urllib 网络请求"),
    (r"\bsocket\.connect\s*\(", 15, "socket 网络连接"),
    (r"\bhttp\.client\s*\(", 12, "http.client 网络请求"),
    (r"(?i)\bInvoke-WebRequest\b", 15, "Invoke-WebRequest 网络请求"),
    (r"(?i)\bInvoke-RestMethod\b", 15, "Invoke-RestMethod 网络请求"),
    (r"(?i)\bcurl(\.exe)?\b", 12, "curl 网络请求"),
    (r"(?i)\bwget\b", 12, "wget 网络请求"),
    # -- 文件系统 (绝对路径写) — Unix + Windows --
    (r'\bopen\s*\(\s*["\'][A-Za-z]:[\\/]', 12, "Windows 绝对路径文件操作"),
    (r'\bopen\s*\(\s*["\']/', 10, "Unix 绝对路径文件操作"),
    (r'\bopen\s*\(\s*["\']\\\\', 12, "UNC 网络路径文件操作"),
    (r"\bPath\s*\(\s*['\"][A-Za-z]:[\\/]", 10, "Windows 绝对路径 Path 操作"),
    (r"\bPath\s*\(\s*['\"]/", 8, "Unix 绝对路径 Path 操作"),
    (r"\bPath\s*\(\s*['\"]\\\\", 10, "UNC 网络路径 Path 操作"),
    (r"\bshutil\.(rmtree|move|copy|copy2)\s*\(", 12, "shutil 高危文件操作"),
    (r"\bos\.(remove|unlink|rmdir|rename|replace)\s*\(", 10, "os 文件删除/重命名"),
    (r"\bos\.(makedirs|mkdir)\s*\(", 6, "os 创建目录"),
    (r"(?i)(?:Remove|Move|Copy|New)-(?:Item|ItemProperty)\s+-", 10, "PowerShell 文件系统修改"),
    # -- 环境变量/密钥/凭据 --
    (r"\bos\.environ\s*\[", 8, "环境变量访问"),
    (r"\bgetpass\.(getpass|getuser)\s*\(", 5, "密码/用户获取"),
    (r"(?i)\bGet-Content\b.*(?:secrets?|\.env|token|key|password)", 10, "读取敏感文件"),
    (r"(?i)(?:api[_-]?key|secret|token|password)\s*[=:]\s*['\"]", 8, "硬编码凭据"),
    # -- 代码下载执行 (supply-chain 最高危) --
    (r"(?i)\b(?:pip|uv|conda)\s+install\b", 18, "包管理器安装新依赖"),
    (r"(?i)(?:npm|pnpm|yarn|bun)\s+(?:add|install)\b", 15, "前端包管理器安装新依赖"),
    (r"\bexec\(open\(", 25, "exec+open 直接执行外部脚本"),
    (r"(?i)\bDownloadString\b.*\bInvoke-Expression\b", 30, "下载并执行 (PowerShell 高危)"),
    (r"(?i)\bcurl[^(\n]{0,200}\|\s*(?:bash|sh|zsh|python|powershell)", 30, "管道执行远程脚本"),
]


def analyze_skill_content(content: str) -> dict[str, Any]:
    """静态分析 Skill 内容 (SKILL.md body + 内嵌 scripts), 返回风险报告。

    返回:
    {
        "score": 35,          # 0-100, 越高越危险
        "level": "medium",    # low / medium / high / critical
        "findings": [
            {"pattern": "eval()", "weight": 30, "label": "eval() 动态执行", "count": 2},
            ...
        ],
        "recommendation": "..."
    }
    """
    findings: list[dict[str, Any]] = []
    total_score = 0

    for pattern, weight, label in _RISK_PATTERNS:
        matches = re.findall(pattern, content, re.IGNORECASE)
        if matches:
            count = len(matches)
            findings.append({
                "pattern": pattern,
                "weight": weight,
                "label": label,
                "count": count,
            })
            total_score += weight * min(count, 3)  # 同类最多算 3 次

    # 上限 100
    total_score = min(total_score, 100)

    if total_score <= 20:
        level = "low"
        recommendation = "低风险: 只读操作为主, 可安全导入。"
    elif total_score <= 50:
        level = "medium"
        recommendation = "中风险: 含文件写操作, 建议审查后导入。"
    elif total_score <= 80:
        level = "high"
        recommendation = "高风险: 含 shell 执行或网络请求, 务必人工审查。"
    else:
        level = "critical"
        recommendation = "极高风险: 含动态执行 + shell + 网络, 强烈建议不要导入。"

    return {
        "score": total_score,
        "level": level,
        "findings": findings,
        "recommendation": recommendation,
    }


def analyze_skill_dir(skill_dir: str | Path) -> dict[str, Any]:
    """分析整个 skill 目录 (SKILL.md + 所有 .py 文件)。"""
    skill_dir = Path(skill_dir)
    parts: list[str] = []

    # SKILL.md body
    md = skill_dir / "SKILL.md"
    if md.exists():
        parts.append(md.read_text(encoding="utf-8", errors="ignore"))

    # 所有 Python 文件
    for py in skill_dir.rglob("*.py"):
        try:
            parts.append(py.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue

    # 所有 shell 脚本
    for sh in skill_dir.rglob("*.sh"):
        try:
            parts.append(sh.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue

    content = "\n".join(parts)
    return analyze_skill_content(content)
