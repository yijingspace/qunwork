"""Skill.lock — 依赖工具签名哈希锁 + 自动生成。

在 SKILL.md 旁边自动生成 skill.lock 文件, 记录依赖的工具签名
(tool_name + 参数 schema 哈希)。当 MCP server 或连接器版本升级后,
lock 文件能检测到工具签名变化, 触发兼容性测试。

skill.lock 格式 (JSON):
{
  "skill_name": "my-skill",
  "skill_version": "0.1.0",
  "generated_at": 1700000000,
  "schema_version": 1,
  "tools": [
    {
      "name": "github__create_pr",
      "schema_hash": "a1b2c3...",
      "params": ["title", "body", "head", "base"],
      "source": "allowed_tools"   # allowed_tools / body_reference
    }
  ],
  "scripts": [
    {
      "path": "scripts/helper.py",
      "sha256": "deadbeef..."
    }
  ]
}
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Optional


def _hash_schema(params: dict | list | None) -> str:
    """对工具参数 schema 做稳定哈希 (排序键后 JSON 序列化 → SHA256 前 16 位)。"""
    if not params:
        return "0000000000000000"
    try:
        blob = json.dumps(params, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        blob = str(params)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return "0" * 64
    return h.hexdigest()


# -- SKILL.md body 中工具引用的启发式提取 -----------------------------------
# 常见模式: `tool_name(...)`、`tools: ["a", "b"]`、`tool_name` in backticks
_TOOL_CALL_RE = re.compile(r"`([a-zA-Z_][\w:.\-]{1,80})\s*\(")
_TOOL_BACKTICK_RE = re.compile(r"`([a-zA-Z_][\w:.\-]{1,80})`")
_TOOL_JSON_LIST_RE = re.compile(r"(?:allowed|required)?-?tools\s*[:=]\s*\[([^\]]{3,2000})\]", re.I)


def extract_tool_refs_from_body(body: str) -> list[str]:
    """启发式从 SKILL.md 正文里提取被引用的工具名。"""
    found: set[str] = set()
    for m in _TOOL_CALL_RE.findall(body):
        found.add(m)
    for m in _TOOL_JSON_LIST_RE.findall(body):
        for token in re.findall(r"['\"]([^'\"]{1,80})['\"]", m):
            token = token.strip()
            if token and 1 < len(token) <= 80:
                found.add(token)
    for m in _TOOL_BACKTICK_RE.findall(body):
        if re.search(r"[\s,.;:!?()[\]{}]", m):
            continue
        # 只保留看起来像工具名的 (含 __ / . 或长于 6 个字符)
        if "__" in m or "." in m or len(m) >= 6:
            found.add(m)
    return sorted(found)


def generate_lock(
    skill_name: str,
    skill_version: str,
    tools: list[dict[str, Any]],
    scripts: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """生成 skill.lock 内容。

    tools 是 [{"name": "github__create_pr", "params": {...}, "source": "..."}] 列表。
    params 可以是 JSON schema dict 或参数名列表；允许省略。
    scripts 是 [{"path": "...", "sha256": "..."}, ...] 列表。
    """
    tool_locks = []
    for t in tools:
        name = t.get("name", "")
        if not name:
            continue
        params = t.get("params") or t.get("parameters") or {}
        if isinstance(params, dict):
            param_names = list(params.get("properties", {}).keys())
        elif isinstance(params, list):
            param_names = [str(x) for x in params]
        else:
            param_names = []
        tool_locks.append({
            "name": name,
            "schema_hash": _hash_schema(params),
            "params": param_names,
            "source": t.get("source", "unknown"),
        })
    return {
        "schema_version": 1,
        "skill_name": skill_name,
        "skill_version": skill_version,
        "generated_at": time.time(),
        "tools": tool_locks,
        "scripts": scripts or [],
    }


def save_lock(skill_dir: str | Path, lock_data: dict) -> Path:
    """将 lock 数据写入 skill 目录下的 skill.lock 文件。"""
    skill_dir = Path(skill_dir)
    lock_path = skill_dir / "skill.lock"
    lock_path.write_text(
        json.dumps(lock_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return lock_path


def load_lock(skill_dir: str | Path) -> Optional[dict]:
    """读取 skill 目录下的 skill.lock, 不存在或损坏返回 None。"""
    lock_path = Path(skill_dir) / "skill.lock"
    if not lock_path.exists():
        return None
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    # 早期 lock 无 schema_version → 视为 0
    data.setdefault("schema_version", 0)
    data.setdefault("tools", [])
    data.setdefault("scripts", [])
    return data


def verify_lock(
    lock_data: dict,
    current_tools: list[dict[str, Any]],
) -> dict[str, Any]:
    """对比 lock 文件与当前工具签名, 返回兼容性报告。

    current_tools: [{"name": "github__create_pr", "params": {...}}]
    """
    locked: dict[str, dict] = {}
    for t in lock_data.get("tools", []):
        # C17: a malformed lock entry (missing "name") must not KeyError the
        # whole compatibility check — skip it instead.
        if not isinstance(t, dict) or not t.get("name"):
            continue
        locked[t["name"]] = t
    current: dict[str, dict] = {}
    for t in current_tools:
        name = t.get("name", "")
        if not name:
            continue
        params = t.get("params") or t.get("parameters") or {}
        if isinstance(params, dict):
            pnames = list(params.get("properties", {}).keys())
        elif isinstance(params, list):
            pnames = [str(x) for x in params]
        else:
            pnames = []
        current[name] = {
            "name": name,
            "schema_hash": _hash_schema(params),
            "params": pnames,
        }

    missing = [n for n in locked if n not in current]
    changed = []
    for n in locked:
        if n in current and locked[n]["schema_hash"] != current[n]["schema_hash"]:
            changed.append({
                "name": n,
                "locked_hash": locked[n]["schema_hash"],
                "current_hash": current[n]["schema_hash"],
                "locked_params": locked[n].get("params", []),
                "current_params": current[n].get("params", []),
            })
    new_tools = [n for n in current if n not in locked]

    return {
        "compatible": len(missing) == 0 and len(changed) == 0,
        "missing_tools": missing,
        "changed_tools": changed,
        "new_tools": new_tools,
    }


# -- 高级: 直接从 skill_dir 自动生成 lock (SkillLoader 集成) -----------------
def auto_generate_lock(
    skill_dir: str | Path,
    *,
    skill_name: Optional[str] = None,
    skill_version: Optional[str] = None,
    registry_tool_schemas: Optional[dict[str, dict | list]] = None,
) -> tuple[Path, dict]:
    """扫描 skill_dir, 解析 SKILL.md 提取工具引用 + 脚本清单, 生成并保存 lock。

    registry_tool_schemas: {tool_name: param_schema_dict_or_list} — 当 skill 引用的工具
        在 registry 中存在时, 用其真实 schema 计算哈希; 否则使用空 schema。

    返回 (lock_path, lock_data)。
    """
    skill_dir = Path(skill_dir)
    md = skill_dir / "SKILL.md"
    if not md.exists():
        raise FileNotFoundError(md)
    text = md.read_text(encoding="utf-8")

    # 解析 frontmatter 拿 name/version/allowed_tools
    if skill_name is None:
        m = re.search(r"^name:\s*(.+)$", text, re.M)
        skill_name = m.group(1).strip() if m else skill_dir.name
    if skill_version is None:
        m = re.search(r"^version:\s*(.+)$", text, re.M)
        skill_version = m.group(1).strip() if m else "0.1.0"
    fm_tools: list[str] = []
    m = re.search(r"^(?:allowed-tools|allowed_tools):\s*(.+)$", text, re.M | re.I)
    if m:
        fm_tools = [x.strip() for x in m.group(1).split(",") if x.strip()]

    # body 引用 (如果 frontmatter 为空, 退化为启发式提取)
    body_start = text.find("\n---", 3)
    body = text[body_start + 4 :].lstrip("\n") if body_start != -1 else text
    body_refs = extract_tool_refs_from_body(body)

    seen: dict[str, str] = {}  # tool_name -> source
    for name in fm_tools:
        seen[name] = "allowed_tools"
    for name in body_refs:
        seen.setdefault(name, "body_reference")

    registry_tool_schemas = registry_tool_schemas or {}
    tools_in: list[dict[str, Any]] = []
    for name, source in seen.items():
        schema = registry_tool_schemas.get(name)
        tools_in.append({"name": name, "params": schema or [], "source": source})

    # 所有脚本文件 (.py / .sh / .ps1 / .bat / .js 等) — 记录 sha256 做内容完整性校验
    SCRIPT_EXTS = {".py", ".sh", ".ps1", ".bat", ".cmd", ".js", ".ts", ".rb"}
    scripts = []
    for f in sorted(skill_dir.rglob("*")):
        if not f.is_file():
            continue
        if f.name == "SKILL.md" or f.name == "skill.lock":
            continue
        if f.suffix.lower() in SCRIPT_EXTS:
            try:
                rel = f.relative_to(skill_dir).as_posix()
            except ValueError:
                rel = f.name
            scripts.append({"path": rel, "sha256": _file_sha256(f)})

    lock_data = generate_lock(skill_name, skill_version, tools_in, scripts)
    return save_lock(skill_dir, lock_data), lock_data


def verify_scripts_integrity(lock_data: dict, skill_dir: str | Path) -> list[dict]:
    """验证 skill 目录下脚本文件的 sha256 是否与 lock 记录一致。"""
    skill_dir = Path(skill_dir)
    mismatches = []
    for s in lock_data.get("scripts", []):
        f = skill_dir / s["path"]
        current = _file_sha256(f) if f.is_file() else ""
        if current != s.get("sha256"):
            mismatches.append({
                "path": s["path"],
                "status": "missing" if not f.is_file() else "mismatch",
                "expected_sha256": s.get("sha256", ""),
                "actual_sha256": current,
                "missing": not f.is_file(),
            })
    # 未在 lock 中登记但存在的新脚本
    SCRIPT_EXTS = {".py", ".sh", ".ps1", ".bat", ".cmd", ".js", ".ts", ".rb"}
    known = {s["path"] for s in lock_data.get("scripts", [])}
    for f in sorted(skill_dir.rglob("*")):
        if not f.is_file() or f.name == "SKILL.md" or f.name == "skill.lock":
            continue
        if f.suffix.lower() not in SCRIPT_EXTS:
            continue
        try:
            rel = f.relative_to(skill_dir).as_posix()
        except ValueError:
            rel = f.name
        if rel not in known:
            mismatches.append({
                "path": rel,
                "status": "unregistered",
                "expected_sha256": "",
                "actual_sha256": _file_sha256(f),
                "unregistered": True,
            })
    return mismatches
