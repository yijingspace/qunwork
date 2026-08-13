"""P1-6 Skill.lock — 依赖工具签名哈希锁。

在 SKILL.md 旁边自动生成 skill.lock 文件, 记录依赖的工具签名
(tool_name + 参数 schema 哈希)。当 MCP server 或连接器版本升级后,
lock 文件能检测到工具签名变化, 触发兼容性测试。

skill.lock 格式 (JSON):
{
  "skill_name": "my-skill",
  "skill_version": "0.1.0",
  "generated_at": 1700000000,
  "tools": [
    {
      "name": "github__create_pr",
      "schema_hash": "a1b2c3...",
      "params": ["title", "body", "head", "base"]
    }
  ]
}
"""
from __future__ import annotations

import hashlib
import json
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


def generate_lock(
    skill_name: str,
    skill_version: str,
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    """生成 skill.lock 内容。

    tools 是 [{"name": "github__create_pr", "params": {...}}] 列表。
    params 可以是 JSON schema dict 或参数名列表。
    """
    tool_locks = []
    for t in tools:
        name = t.get("name", "")
        params = t.get("params") or t.get("parameters") or {}
        if isinstance(params, dict):
            param_names = list(params.get("properties", {}).keys())
        elif isinstance(params, list):
            param_names = list(params)
        else:
            param_names = []
        tool_locks.append({
            "name": name,
            "schema_hash": _hash_schema(params),
            "params": param_names,
        })
    return {
        "skill_name": skill_name,
        "skill_version": skill_version,
        "generated_at": time.time(),
        "tools": tool_locks,
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
    """读取 skill 目录下的 skill.lock, 不存在返回 None。"""
    lock_path = Path(skill_dir) / "skill.lock"
    if not lock_path.exists():
        return None
    try:
        return json.loads(lock_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def verify_lock(
    lock_data: dict,
    current_tools: list[dict[str, Any]],
) -> dict[str, Any]:
    """对比 lock 文件与当前工具签名, 返回兼容性报告。

    current_tools: [{"name": "github__create_pr", "params": {...}}]
    返回: {
        "compatible": True/False,
        "missing_tools": [...],       # lock 中有但当前 registry 没有的
        "changed_tools": [...],       # schema_hash 变化的
        "new_tools": [...],           # 当前 registry 有但 lock 中没有的
    }
    """
    locked = {t["name"]: t for t in lock_data.get("tools", [])}
    current = {}
    for t in current_tools:
        name = t.get("name", "")
        params = t.get("params") or t.get("parameters") or {}
        current[name] = {
            "name": name,
            "schema_hash": _hash_schema(params),
            "params": (
                list(params.get("properties", {}).keys())
                if isinstance(params, dict)
                else list(params) if isinstance(params, list) else []
            ),
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
