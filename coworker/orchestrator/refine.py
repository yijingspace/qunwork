"""Refine — 蜂群经验蒸馏 (对标 Prime Agent /refine 的自进化闭环).

输入: 一次蜂群运行的结果 (OrchestrationResult — plan/tasks + 评审结论 +
governance report + status)。
输出: 蒸馏出的 SwarmLesson 集合, 写入 HarnessStore。

蒸馏规则 (全部基于"证据" — 来自运行事实, 不凭空编造):
  * 成功策略 (lesson): 完成的任务 → 提炼"任务类型 → 成功做法"一句话经验,
    标签带任务类型 (如 "report", "research", "code");
  * 教训 (lesson): 失败/停滞/超时的任务 → 提炼"该类型任务为何失败"的经验,
    下次规划时注入, 避免重蹈覆辙 (自进化核心);
  * 技能提示 (skill_hint): 同类型任务多次出现 (>= REPEAT_FOR_SKILL) →
    提示可固化为 skill 的模板 (供用户/管理者决定是否 create_skill);
  * 任务模板 (task_template): 完成度高的任务描述 → 通用模板 (意图模式),
    供未来相似意图直接参考拆解方式;
  * 自造工具 (tool_uses): executor 自造了工具 → 生成"自造工具策略"经验
    (提示下次同类任务可直接复用 create_selfmade_tool / selfmade_tools 库);
    同类型任务重复自造 → 额外生成"可技能化"提示 (固化成 skill)。

幂等: 同 (kind, title) 的经验会更新版本而非重复插入 (harness.add 的
version bump)。dry_run 不写库。
"""

from __future__ import annotations

import re
from typing import Any, Optional

from .harness import HarnessStore, SwarmLesson

# 同一意图模式出现几次后, 提示固化为技能
REPEAT_FOR_SKILL = 2
# 任务描述中提取"任务类型"的最小长度 (中文 2 字 / 英文 3 字母)
_MIN_TYPE_LEN = 2


def _task_type(description: str) -> str:
    """从任务描述里提取粗粒度任务类型: 找中文关键词或英文首词。

    例: "撰写市场报告" → "报告"; "Write a quarterly report" → "report";
        "修复前端登录 bug" → "修复"。
    """
    text = (description or "").strip()
    for kw in ("报告", "调研", "研究", "分析", "编写", "撰写", "代码", "修复", "测试",
               "审查", "设计", "整理", "翻译", "部署", "会议", "计划"):
        if kw in text:
            return kw
    m = re.match(r"([A-Za-z]{3,})", text)
    if m:
        return m.group(1).lower()
    return text[:_MIN_TYPE_LEN] if len(text) >= _MIN_TYPE_LEN else text


def _summarize_intent(intent: str) -> str:
    """意图摘要: 取前 60 字符, 去首尾空白。"""
    return (intent or "").strip()[:60]


def refine_run(
    result: Any,
    harness: HarnessStore,
    *,
    dry_run: bool = False,
    tool_uses: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """从一次蜂群运行结果蒸馏经验并写入 harness。

    result: OrchestrationResult (含 plan.tasks, status, governance_report)。
    tool_uses: 可选 — 本次 run 中 executor 调用的工具清单
        [{"tool": name, "task_id": id, "task_desc": description}] (自造工具蒸馏)。
    返回 {"added": [...], "existing": [...], "dry_run": bool} — added 为新增/
    更新的 lesson dict 列表。
    """
    if harness is None or result is None:
        return {"added": [], "existing": [], "dry_run": dry_run}

    plan = getattr(result, "plan", None)
    tasks = list(getattr(plan, "tasks", []) or [])
    if not tasks:
        return {"added": [], "existing": [], "dry_run": dry_run}

    status = getattr(result, "status", "unknown")
    intent = _summarize_intent(getattr(result, "intent", ""))
    source_run = getattr(result, "run_id", "") or getattr(result, "source_run_id", "")

    added: list[dict[str, Any]] = []
    existing: list[dict[str, Any]] = []

    # 1) 每类任务的成功/失败经验 (lesson)
    task_types: dict[str, list[Any]] = {}
    for t in tasks:
        ttype = _task_type(getattr(t, "description", ""))
        task_types.setdefault(ttype, []).append(t)

    for ttype, members in task_types.items():
        done = [t for t in members if getattr(t, "done", False) and getattr(t, "result", "")]
        failed = [
            t
            for t in members
            if getattr(t, "status", "") in ("needs_human",)
            or (getattr(t, "done", False) and not getattr(t, "result", ""))
        ]
        if done:
            title = f"[成功] {ttype} 任务策略"
            body = (
                f"任务类型「{ttype}」的完成策略 (来自 {source_run or 'swarm run'}"
                f"{', status=' + status if status else ''}):\n"
                + "\n".join(
                    f"- {t.id}: {_clip(getattr(t, 'description', ''))}"
                    for t in done[:3]
                )
                + "\n\n下次遇到同类任务时参考此策略, 可显著缩短探索时间。"
            )
            lesson = _upsert(
                harness, "lesson", title, body, source_run, intent, [ttype], dry_run
            )
            (added if lesson.get("new") else existing).append(lesson)
        if failed:
            title = f"[教训] {ttype} 任务易失败"
            body = (
                f"任务类型「{ttype}」的失败教训 (来自 {source_run or 'swarm run'}"
                f"{', status=' + status if status else ''}):\n"
                + "\n".join(
                    f"- {t.id}: {_clip(getattr(t, 'description', ''))}"
                    for t in failed[:3]
                )
                + "\n\n下次遇到同类任务时注意规避上述风险点。"
            )
            lesson = _upsert(
                harness, "lesson", title, body, source_run, intent, [ttype, "risk"], dry_run
            )
            (added if lesson.get("new") else existing).append(lesson)

    # 2) 高频任务类型 → 技能提示 (skill_hint)
    for ttype, members in task_types.items():
        if len(members) >= REPEAT_FOR_SKILL:
            title = f"[可技能化] {ttype} 任务流程"
            body = (
                f"「{ttype}」类任务在本次运行中出现 {len(members)} 次, 已形成固定流程。"
                f"建议固化为 skill (create_skill), 名称建议: {ttype}-workflow。\n"
                + "重复出现的任务:\n"
                + "\n".join(
                    f"- {_clip(getattr(t, 'description', ''))}" for t in members[:5]
                )
            )
            lesson = _upsert(
                harness, "skill_hint", title, body, source_run, intent, [ttype, "skill"], dry_run
            )
            (added if lesson.get("new") else existing).append(lesson)

    # 3) 通用任务模板 (task_template) — 取完成任务的描述作为可复用模板
    done_tasks = [t for t in tasks if getattr(t, "done", False)]
    if done_tasks:
        title = f"[模板] {intent or 'swarm task'} 任务拆解模式"
        body = (
            f"意图「{intent or '（未命名）'}」的任务拆解模板 (status={status}):\n"
            + "\n".join(
                f"- {t.id} ({_task_type(getattr(t, 'description', ''))}): "
                f"{_clip(getattr(t, 'description', ''))}"
                for t in done_tasks[:8]
            )
            + "\n\n同类意图可直接参考此拆解粒度与依赖结构。"
        )
        lesson = _upsert(
            harness, "task_template", title, body, source_run, intent, ["template"], dry_run
        )
        (added if lesson.get("new") else existing).append(lesson)

    # 4) 自造工具蒸馏 (工具自治): executor 在本次 run 中自造了工具 →
    #    生成"自造工具策略"经验; 同类型任务重复自造 → "可技能化"提示。
    selfmade = [
        u
        for u in (tool_uses or [])
        if (u.get("tool") or "").startswith("create_selfmade_tool")
    ]
    if selfmade:
        # 收集本次 run 自造的工具名 (从 harness 的 skill_hint 或 tool_uses)
        # 任务描述 → 任务类型
        by_type: dict[str, int] = {}
        for u in selfmade:
            ttype = _task_type(u.get("task_desc") or "")
            if ttype:
                by_type[ttype] = by_type.get(ttype, 0) + 1
        for ttype, count in by_type.items():
            title = f"[自造工具] {ttype} 任务自制工具策略"
            body = (
                f"任务类型「{ttype}」在本次运行中自造了 {count} 次工具"
                f"(来自 {source_run or 'swarm run'}):\n"
                + "  - 工具不足时可直接用 create_selfmade_tool 编写 Python 实现, "
                "立即注册并持久化到 workspace/selfmade_tools/, 下次会话自动加载。\n"
                + "  - 自造工具后, 同类型的后续任务可直接复用, 无需重新发明。"
            )
            lesson = _upsert(
                harness,
                "lesson",
                title,
                body,
                source_run,
                intent,
                [ttype, "selfmade_tool"],
                dry_run,
            )
            (added if lesson.get("new") else existing).append(lesson)
            # 重复自造 (>1 次) → 技能提示: 该任务类型的自造工具可固化成 skill
            if count >= 2:
                title2 = f"[可技能化] {ttype} 自造工具流程"
                body2 = (
                    f"「{ttype}」类任务在本次运行中自造了 {count} 个工具, 已形成"
                    f"固定模式。建议将自造工具流程固化为 skill (create_skill), "
                    f"或直接复用 workspace/selfmade_tools/ 下已持久化的工具。"
                )
                lesson2 = _upsert(
                    harness,
                    "skill_hint",
                    title2,
                    body2,
                    source_run,
                    intent,
                    [ttype, "selfmade_tool", "skill"],
                    dry_run,
                )
                (added if lesson2.get("new") else existing).append(lesson2)

    return {"added": added, "existing": existing, "dry_run": dry_run}


def harness_context(harness: Optional[HarnessStore], intent: str, k: int = 5) -> str:
    """为下次规划生成"经验上下文"块 (与 HORNET 共振上下文同机制)。

    检索与 intent 相关的历史经验, 返回一段注入 planner 输入的文本。
    """
    if harness is None or not (intent or "").strip():
        return ""
    hits = harness.search(intent, k=k)
    if not hits:
        return ""
    for h in hits:
        try:
            if h.id is not None:
                harness.bump_usage(h.id)
        except Exception:
            pass
    lines = []
    for h in hits:
        lines.append(f"  - [{h.kind}] {h.title}: {_clip(h.body, 120)}")
    return "[蜂群经验 (harness) — 来自历史 swarm 运行的教训与策略]\n" + "\n".join(lines) + "\n\n"


def _clip(text: str, max_len: int = 100) -> str:
    text = (text or "").strip()
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def _upsert(
    harness: HarnessStore,
    kind: str,
    title: str,
    body: str,
    source_run: str,
    intent: str,
    tags: list[str],
    dry_run: bool,
) -> dict[str, Any]:
    if dry_run:
        return {"id": None, "kind": kind, "title": title, "new": True}
    lesson = harness.add(
        kind=kind, title=title, body=body, source_run_id=source_run, intent=intent, tags=tags
    )
    is_new = lesson.version == 1 and lesson.use_count == 0
    return {
        "id": lesson.id,
        "kind": kind,
        "title": title,
        "version": lesson.version,
        "new": is_new,
    }
