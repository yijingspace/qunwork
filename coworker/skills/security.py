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
import threading
from pathlib import Path
from typing import Any

# 参与目录扫描的可执行脚本 glob (C15: 覆盖 PowerShell/批处理/JS, 防纯 .ps1 恶意 skill 得 0 分)
_SCRIPT_GLOBS = ("*.py", "*.sh", "*.ps1", "*.psm1", "*.bat", "*.cmd", "*.js", "*.ts", "*.rb")

# 目录扫描结果缓存: {目录路径 -> {"fingerprint": 指纹, "result": 分析结果}}。
# key 稳定为目录路径 (不再拼接全量文件指纹, 大目录 key 计算本身贵),
# mtime 指纹在值内做快速失效检查; 上限 512 防膨胀 (真实环境 64+ 技能)。
_DIR_SCAN_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_LOCK = threading.Lock()


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


def level_for_score(total_score: int) -> str:
    """Map a 0-100 risk score to a severity level — the SINGLE source of truth.

    Score semantics: HIGHER score = MORE dangerous. Both analyze_skill_content
    and the catalog's security_level field must agree; keep them in lockstep
    (regression: catalog_row once inverted this mapping, showing the most
    dangerous skills as 'low')."""
    if total_score <= 20:
        return "low"
    if total_score <= 50:
        return "medium"
    if total_score <= 80:
        return "high"
    return "critical"


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

    level = level_for_score(total_score)
    if level == "low":
        recommendation = "低风险: 只读操作为主, 可安全导入。"
    elif level == "medium":
        recommendation = "中风险: 含文件写操作, 建议审查后导入。"
    elif level == "high":
        recommendation = "高风险: 含 shell 执行或网络请求, 务必人工审查。"
    else:
        recommendation = "极高风险: 含动态执行 + shell + 网络, 强烈建议不要导入。"

    return {
        "score": total_score,
        "level": level,
        "findings": findings,
        "recommendation": recommendation,
    }


def analyze_skill_dir(skill_dir: str | Path) -> dict[str, Any]:
    """分析整个 skill 目录 (SKILL.md + 所有 .py 文件)。

    结果按目录路径缓存 (进程内), 配 mtime 指纹做快速失效检查。每个会话/
    引擎构建都会新建 SkillLoader 并对每个技能跑一次分析 — 真实用户环境有
    64+ 技能 (含 HORNET 涌现), img2threejs 单目录 260 文件, 全量重扫把
    会话建立拖到 ~9s (2026-08-18 排查: 主会话发消息 LLM 无反应)。缓存键
    不再拼接全部文件指纹 (那样大目录的 key 计算本身就贵, 且缓存上限会挤掉
    大技能), 而是 key=目录路径 + 值内带 mtime 指纹; 文件未变直接复用,
    变了才重扫。
    """
    skill_dir = Path(skill_dir)
    try:
        resolved = str(skill_dir.resolve())
    except OSError:
        resolved = str(skill_dir)

    with _CACHE_LOCK:
        entry = _DIR_SCAN_CACHE.get(resolved)
        if entry is not None and entry["fingerprint"] == _dir_fingerprint(skill_dir):
            return entry["result"]

    result = _analyze_skill_dir_uncached(skill_dir)

    with _CACHE_LOCK:
        # 上限按技能数放宽: 真实环境 64+ 技能, 旧 64 上限把大技能条目逐出,
        # 每次都重扫 img2threejs (它 260 文件最贵)。512 足够覆盖任何合理部署。
        if len(_DIR_SCAN_CACHE) >= 512:
            _DIR_SCAN_CACHE.pop(next(iter(_DIR_SCAN_CACHE)))
        _DIR_SCAN_CACHE[resolved] = {
            "fingerprint": _dir_fingerprint(skill_dir),
            "result": result,
        }
    return result


def _dir_fingerprint(skill_dir: Path) -> tuple:
    """目录内容指纹: SKILL.md + 各 glob 首个文件的 (mtime_ns, size)。

    不做全量 stat — 大技能 (img2threejs 260 文件) 全量 stat 本身就 ~0.1s,
    乘以 64 技能就回到秒级。SKILL.md 是技能主体, 加每个 glob 最新文件即可
    捕获新增/删除脚本; 命中率远超漏检率 (文件内容改但 mtime 没变仅发生在
    git checkout 场景, 可接受)。
    """
    marks: list[tuple[str, int, int]] = []
    md = skill_dir / "SKILL.md"
    try:
        st = md.stat()
        marks.append(("SKILL.md", st.st_mtime_ns, st.st_size))
    except OSError:
        pass
    for glob in _SCRIPT_GLOBS:
        latest: Optional[tuple[str, int, int]] = None
        try:
            for f in skill_dir.rglob(glob):
                try:
                    s = f.stat()
                    cand = (f.name, s.st_mtime_ns, s.st_size)
                    if latest is None or cand[1] > latest[1]:
                        latest = cand
                except OSError:
                    continue
        except OSError:
            continue
        if latest is not None:
            marks.append((glob, latest[1], latest[2]))
    return tuple(marks)


def _analyze_skill_dir_uncached(skill_dir: Path) -> dict[str, Any]:
    parts: list[str] = []

    # SKILL.md body
    md = skill_dir / "SKILL.md"
    if md.exists():
        parts.append(md.read_text(encoding="utf-8", errors="ignore"))

    # 所有可执行脚本 — Python + shell + PowerShell/批处理/JS 等。规则表里有
    # 大量 PowerShell / cmd.exe / Invoke-WebRequest 模式, 只扫 *.py/*.sh 会让
    # 纯 .ps1 恶意 skill 得 0 分 (C15)。
    for glob in _SCRIPT_GLOBS:
        for script in skill_dir.rglob(glob):
            try:
                parts.append(script.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                continue

    content = "\n".join(parts)
    return analyze_skill_content(content)
