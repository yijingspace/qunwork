"""Skill 兼容性测试引擎 — 检查 skill 依赖的工具 schema 变化 + 自动修复建议。

当 MCP server 或连接器版本升级后:
1. 加载 skill.lock, 对比当前 registry 中工具签名
2. 生成缺失/变化/新增工具的可读报告
3. *自动修复*: 把所有不兼容 skill 打包成一个 Swarm reviewer 任务
   (让 reviewer 逐 skill 分析, 给出修改 SKILL.md allowed-tools / 更新参数映射
   / 重写 lock 等建议)
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .lock import load_lock, verify_lock


@dataclass
class CompatibilityReport:
    skill_name: str
    skill_version: str = ""
    skill_path: Optional[str] = None
    has_lock: bool = False
    compatible: bool = True
    missing_tools: list[str] = None  # type: ignore[assignment]
    changed_tools: list[dict] = None  # type: ignore[assignment]
    new_tools: list[str] = None  # type: ignore[assignment]
    recommendation: str = ""
    # 由 build_autofix_prompt 生成的 reviewer prompt
    autofix_plan: Optional[dict[str, Any]] = None

    def __post_init__(self):
        if self.missing_tools is None:
            self.missing_tools = []
        if self.changed_tools is None:
            self.changed_tools = []
        if self.new_tools is None:
            self.new_tools = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "skill_version": self.skill_version,
            "skill_path": self.skill_path,
            "has_lock": self.has_lock,
            "compatible": self.compatible,
            "missing_tools": list(self.missing_tools),
            "changed_tools": [dict(c) for c in self.changed_tools],
            "new_tools": list(self.new_tools),
            "recommendation": self.recommendation,
            "autofix_plan": self.autofix_plan,
            "severity": self.severity(),
        }

    def severity(self) -> str:
        """low / medium / high — 决定 UI 标色。"""
        if self.has_lock and self.compatible:
            return "low"
        if not self.has_lock:
            return "medium"
        if self.missing_tools:
            return "high"
        return "medium"


def _build_recommendation(report: "CompatibilityReport", lock_data: dict) -> str:
    if not report.has_lock:
        return (
            "未找到 skill.lock, 无法验证兼容性。建议点击「生成锁文件」后再检查。"
        )
    if report.compatible:
        return (
            f"所有 {len(lock_data.get('tools', []))} 个锁定工具签名与当前 registry 匹配, "
            "兼容性正常。"
        )
    parts = []
    if report.missing_tools:
        parts.append(
            "缺失工具: "
            + ", ".join(f"`{t}`" for t in report.missing_tools)
            + " (对应连接器已卸载 / MCP server 已移除 / 注册表中重命名。"
            "自动修复会让 reviewer 分析 skill 是否可以改为使用其他工具。)"
        )
    if report.changed_tools:
        for ct in report.changed_tools:
            removed = sorted(set(ct.get("locked_params", [])) - set(ct.get("current_params", [])))
            added = sorted(set(ct.get("current_params", [])) - set(ct.get("locked_params", [])))
            chunk = f"工具 `{ct['name']}` 参数变化:"
            if removed:
                chunk += f" 移除参数 {', '.join(removed)}"
            if added:
                chunk += f" 新增参数 {', '.join(added)}"
            if not removed and not added:
                chunk += " (仅 schema 内部结构变化, 同名参数属性已调整)"
            parts.append(chunk)
    if report.new_tools:
        parts.append(
            "新可用工具(lock中未登记, 可使用): "
            + ", ".join(f"`{t}`" for t in report.new_tools[:20])
            + (" …" if len(report.new_tools) > 20 else "")
        )
    parts.append("可点击「一键申请自动修复」让 Swarm reviewer 角色生成建议修改。")
    return "  \n".join(parts)


def check_skill_compatibility(
    skill_dir: str | Path,
    registry_tools: list[dict[str, Any]],
) -> dict[str, Any]:
    """检查单个 skill 兼容性。

    Args:
        skill_dir: skill 目录 (含 SKILL.md 和可选 skill.lock)。
        registry_tools: 当前 registry 中所有工具的 [{"name", "params"}]。

    返回 CompatibilityReport.to_dict() 格式。
    """
    skill_dir = Path(skill_dir)
    lock_data = load_lock(skill_dir)
    report = CompatibilityReport(
        skill_name=skill_dir.name,
        skill_path=str(skill_dir),
    )

    if not lock_data:
        report.has_lock = False
        report.compatible = True
        report.recommendation = _build_recommendation(report, {})
        return report.to_dict()

    report.has_lock = True
    report.skill_name = lock_data.get("skill_name", skill_dir.name)
    report.skill_version = lock_data.get("skill_version", "")
    vr = verify_lock(lock_data, registry_tools)
    report.compatible = vr["compatible"]
    report.missing_tools = list(vr["missing_tools"])
    report.changed_tools = [dict(c) for c in vr["changed_tools"]]
    report.new_tools = list(vr["new_tools"])
    report.recommendation = _build_recommendation(report, lock_data)
    # 如果不兼容, 顺便生成 reviewer 任务骨架 (不自动执行)
    if not report.compatible:
        report.autofix_plan = build_autofix_plan(report, lock_data)
    return report.to_dict()


def check_all_skills(
    skill_dirs: list[str | Path],
    registry_tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """批量检查多个 skill 目录, 按 severity 严重到轻排序。"""
    results = []
    for d in skill_dirs:
        p = Path(d)
        # 目录下一层直接包含 SKILL.md? 否则遍历子目录
        if (p / "SKILL.md").is_file():
            results.append(check_skill_compatibility(p, registry_tools))
        elif p.is_dir():
            for sub in sorted(p.iterdir()):
                if (sub / "SKILL.md").is_file():
                    results.append(check_skill_compatibility(sub, registry_tools))
    severity_rank = {"high": 0, "medium": 1, "low": 2}
    results.sort(key=lambda r: (severity_rank.get(r["severity"], 9), r["skill_name"]))
    return results


# -- 自动修复计划: 给 Swarm reviewer 角色用的 prompt ------------------------
def build_autofix_plan(
    report: CompatibilityReport, lock_data: dict
) -> dict[str, Any]:
    """根据不兼容的原因, 生成 reviewer 角色的修复计划 (Swarm 任务)。

    返回:
    {
      "title": "...",
      "intent": "... reviewer 读入的完整意图文字",
      "artifacts": ["SKILL.md 中的 allowed-tools", "参数映射段落", "重写 skill.lock"],
      "reviewer_rubric": [...],  # reviewer 判分条目
    }
    """
    lines: list[str] = []
    lines.append(
        f"# Skill [{report.skill_name} v{report.skill_version}] 兼容性修复"
    )
    lines.append("")
    lines.append("## 检测到的问题")
    if report.missing_tools:
        lines.append(
            "- 工具缺失 (注册表中找不到, 可能连接器卸载/MCP server移除):"
            + ", ".join(report.missing_tools)
        )
    for ct in report.changed_tools:
        removed = sorted(set(ct.get("locked_params", [])) - set(ct.get("current_params", [])))
        added = sorted(set(ct.get("current_params", [])) - set(ct.get("locked_params", [])))
        lines.append(
            f"- 工具 `{ct['name']}` schema 变更 (hash {ct['locked_hash'][:8]}→{ct['current_hash'][:8]}): "
            f"移除参数 {removed or '无'}; 新增参数 {added or '无'}"
        )
    if report.new_tools:
        lines.append("- 新增可用工具 (lock 未登记, 可供 skill 重新规划使用): " + ", ".join(report.new_tools[:20]))

    lines.append("")
    lines.append("## 你的任务 (Reviewer Agent)")
    lines.append(
        "读取该 skill 目录下的 SKILL.md, 分析它在哪些步骤使用了以上工具。"
    )
    lines.append("输出一个 JSON 修复方案, 含以下字段:")
    lines.append(
        "- `updated_frontmatter`: {\"allowed-tools\": [...], ...} 需要更新的 frontmatter; "
        "如果 tool 完全移除则删除对应条目; 如果有替代工具则写上替代。"
    )
    lines.append(
        "- `parameter_mappings`: {\"old_tool.old_arg\": \"new_tool.new_arg\"} 当参数名/顺序变化时给出映射。"
    )
    lines.append(
        "- `body_edits`: [ {\"search\": \"原句片段\", \"replace\": \"修改后的建议\"} ] SKILL.md 正文的逐段修改。"
    )
    lines.append(
        "- `regenerate_lock`: true/false — 方案提交后是否需要重新生成 skill.lock。"
    )
    lines.append(
        "- `risk_level_estimate`: low / medium / high — 你的方案对 skill 行为的破坏性评估。"
    )
    lines.append("- `why_this_works`: 50 字以内说明修复思路的合理性。")

    reviewer_rubric = [
        ("覆盖所有缺失工具", "是否为每个 missing tool 都给出替代或禁用理由"),
        ("映射完整性", "是否为每个 changed 参数都列出了映射或降级处理"),
        ("向后兼容", "skill 的对外行为 (输入/输出语义) 是否保持与文档一致"),
        ("lock 更新", "是否正确标注了 regenerate_lock 必要性"),
        ("风险声明", "risk_level_estimate 是否与 body_edits 的破坏性一致"),
    ]

    return {
        "title": f"Repair skill {report.skill_name} compat",
        "intent": "\n".join(lines),
        "artifacts": [
            "updated_frontmatter (SKILL.md frontmatter 新键值)",
            "parameter_mappings (参数名映射)",
            "body_edits (正文修改建议)",
            "regenerate_lock (是否重写 lock)",
        ],
        "reviewer_rubric": reviewer_rubric,
        "input_artifacts": {"skill_path": report.skill_path, **lock_data},
    }


def build_batch_autofix(
    reports: list[dict[str, Any]],
    output: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    """把批量不兼容 skill 合并成一个大的 Swarm 多任务 DAG 意图。

    Args:
        reports: check_all_skills 返回的列表。
        output: 可选日志打印回调。

    返回:
        {
          "task_intents": [...],  # 每个 skill 对应一个 DAG task
          "global_intent": "运行 reviewer 角色批量修复 ... 整体文本",
          "plan": [{"id": "t0", "description": "...", "deps": []}, ...]
        }
    """
    broken = [r for r in reports if not r.get("compatible", True) and r.get("has_lock")]
    unlocked = [r for r in reports if not r.get("has_lock")]
    plans: list[dict[str, Any]] = []
    intents: list[str] = []
    for i, r in enumerate(broken):
        cr = CompatibilityReport(
            skill_name=r["skill_name"],
            skill_version=r.get("skill_version", ""),
            skill_path=r.get("skill_path"),
            has_lock=True,
            compatible=False,
            missing_tools=list(r.get("missing_tools", [])),
            changed_tools=list(r.get("changed_tools", [])),
            new_tools=list(r.get("new_tools", [])),
        )
        lock = load_lock(cr.skill_path or "") or {}
        plan = build_autofix_plan(cr, lock)
        intents.append(plan["intent"])
        plans.append({"id": f"t{i}", "description": f"修复 {cr.skill_name}", "deps": []})

    if unlocked and output:
        output(f"注意: {len(unlocked)} 个 skill 没有 lock, 跳过自动修复计划。")

    global_intent = (
        "Swarm 任务: 以下是一个批量兼容性修复工作流。"
        f"涉及 {len(plans)} 个不兼容 skill, 所有任务并行执行。\n\n"
        "=== 每个任务意图如下 (按顺序):\n\n"
        + "\n\n---\n\n".join(intents)
    )
    return {
        "count_broken": len(broken),
        "count_unlocked": len(unlocked),
        "task_intents": intents,
        "plan": plans,
        "global_intent": global_intent,
    }
