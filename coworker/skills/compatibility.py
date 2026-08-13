"""P1-6 Skill 兼容性测试 — 检查 skill 依赖的工具是否仍可用。

当 MCP server 或连接器版本升级后, 自动跑兼容性测试:
1. 检查 lock 文件中记录的工具是否仍存在于 registry
2. 对比参数 schema 哈希是否变化
3. 标红不兼容的 skill, 提供修复建议
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .lock import load_lock, verify_lock


def check_skill_compatibility(
    skill_dir: str | Path,
    registry_tools: list[dict[str, Any]],
) -> dict[str, Any]:
    """检查单个 skill 的兼容性。

    skill_dir: skill 目录路径 (含 SKILL.md 和可选的 skill.lock)
    registry_tools: 当前 registry 中所有工具的 [{"name", "params"}]

    返回:
    {
        "skill_name": "my-skill",
        "has_lock": True,
        "compatible": True/False,
        "missing_tools": [...],
        "changed_tools": [...],
        "new_tools": [...],
        "recommendation": "..."
    }
    """
    skill_dir = Path(skill_dir)
    lock_data = load_lock(skill_dir)

    if not lock_data:
        # 没有 lock 文件 → 无法验证, 标记为 "unlocked"
        return {
            "skill_name": skill_dir.name,
            "has_lock": False,
            "compatible": True,  # 无 lock 不阻断, 只是无法验证
            "recommendation": "未找到 skill.lock, 建议运行「生成锁文件」以启用兼容性检查。",
        }

    report = verify_lock(lock_data, registry_tools)

    if report["compatible"]:
        recommendation = "所有依赖工具签名匹配, 兼容性正常。"
    else:
        parts = []
        if report["missing_tools"]:
            parts.append(
                f"缺失工具: {', '.join(report['missing_tools'])} "
                f"(可能对应的连接器已卸载或 MCP server 已移除)"
            )
        if report["changed_tools"]:
            for ct in report["changed_tools"]:
                parts.append(
                    f"工具签名变化: {ct['name']} "
                    f"(lock 参数: {ct['locked_params']} → 当前: {ct['current_params']})"
                )
        recommendation = " ⚠ ".join(parts) + " 建议更新 skill 或重新生成 lock 文件。"

    return {
        "skill_name": lock_data.get("skill_name", skill_dir.name),
        "skill_version": lock_data.get("skill_version", ""),
        "has_lock": True,
        "compatible": report["compatible"],
        "missing_tools": report["missing_tools"],
        "changed_tools": report["changed_tools"],
        "new_tools": report["new_tools"],
        "recommendation": recommendation,
    }


def check_all_skills(
    skill_dirs: list[str | Path],
    registry_tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """批量检查多个 skill 目录的兼容性。"""
    results = []
    for d in skill_dirs:
        p = Path(d)
        if not p.is_dir():
            continue
        results.append(check_skill_compatibility(p, registry_tools))
    return results
