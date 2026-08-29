"""LoopCoop 自动重规划器 (突破方案二 · 自动重规划).

当编排循环收敛停滞 (stall) 或评审连续不通过时, 不再死循环重试原任务, 而是:
  1. 诊断失败任务 (评审不通过 / 超时 / 重试耗尽);
  2. 提出重规划动作: 重述任务 (reword) / 拆分任务 (split) / 合并任务 (merge) /
     删除冗余任务 (drop) / 新增修复任务 (patch);
  3. 返回修订后的任务列表 (与 Plan.tasks 同构: id/description/deps/agent/…),
     由编排器替换原计划继续执行。

与 B7 (无优雅降级) 对应: 重规划是"任务级降级"的第一层 — 任务失败后先改任务,
而非整体失败。与 convergence.py 搭配: ``is_stalled()`` → ``suggest()``。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

ACTION_REWORD = "reword"
ACTION_SPLIT = "split"
ACTION_MERGE = "merge"
ACTION_DROP = "drop"
ACTION_PATCH = "patch"


class Replanner:
    """启发式任务重规划器 — 纯规则, 无 LLM 依赖, 可测试。

    ``suggest(plan, failed_tasks, reason) -> list[dict]`` 返回修订任务建议;
    调用方 (Orchestrator 集成点) 决定是否采纳。
    """

    def __init__(self, *, max_tasks: int = 16, split_threshold: int = 3) -> None:
        self.max_tasks = max_tasks
        self.split_threshold = split_threshold  # 同一任务失败 N 次 → 拆分

    def diagnose(self, plan: Any, failed_tasks: list[Any]) -> list[dict[str, Any]]:
        """诊断每个失败任务, 输出建议动作。"""
        tasks = plan.tasks if hasattr(plan, "tasks") else []
        by_id = {t.id: t for t in tasks} if hasattr(tasks, "__iter__") else {}
        suggestions: list[dict[str, Any]] = []
        for t in failed_tasks:
            tid = getattr(t, "id", None) or (t.get("id") if isinstance(t, dict) else None)
            retries = getattr(t, "retries", 0) if not isinstance(t, dict) else t.get("retries", 0)
            if tid is None:
                continue
            if retries >= self.split_threshold:
                suggestions.append(
                    {"task_id": tid, "action": ACTION_SPLIT, "reason": f"retried {retries}x"}
                )
            else:
                suggestions.append(
                    {"task_id": tid, "action": ACTION_REWORD, "reason": "review rejected"}
                )
        return suggestions

    def suggest(
        self, plan: Any, failed_tasks: list[Any], *, reason: str = ""
    ) -> list[dict[str, Any]]:
        """生成修订任务列表 (仅含被建议变更的任务; 保持原任务 id 不变)。

        返回的每项与原 Task 同构: {"id", "description", "deps", "action"} —
        由编排器根据 action 决定替换描述还是拆分。
        """
        suggestions = self.diagnose(plan, failed_tasks)
        out: list[dict[str, Any]] = []
        for s in suggestions:
            task = self._find(plan, s["task_id"])
            if task is None:
                continue
            desc = getattr(task, "description", "") if not isinstance(task, dict) else task.get("description", "")
            deps = getattr(task, "deps", []) if not isinstance(task, dict) else task.get("deps", [])
            if s["action"] == ACTION_SPLIT:
                parts = self._split(desc, s["task_id"], deps)
                out.extend(parts)
            elif s["action"] == ACTION_REWORD:
                out.append(
                    {
                        "id": s["task_id"],
                        "description": f"[重规划] {desc}",
                        "deps": list(deps),
                        "action": ACTION_REWORD,
                    }
                )
            elif s["action"] == ACTION_DROP:
                out.append(
                    {"id": s["task_id"], "description": desc, "deps": list(deps), "action": ACTION_DROP}
                )
            elif s["action"] == ACTION_PATCH:
                out.append(
                    {
                        "id": f"{s['task_id']}-patch",
                        "description": f"[修复] {desc}",
                        "deps": [s["task_id"]],
                        "action": ACTION_PATCH,
                    }
                )
        return out

    def _find(self, plan: Any, task_id: str) -> Optional[Any]:
        for t in plan.tasks if hasattr(plan, "tasks") else []:
            tid = getattr(t, "id", None)
            if tid == task_id:
                return t
        return None

    def _split(self, desc: str, task_id: str, deps: list) -> list[dict]:
        """把任务描述按句子/顿号拆成两个子任务 (a/b)。"""
        text = desc
        for sep in ("；", ";", "。", "，", ", "):
            if sep in text:
                a, _, b = text.partition(sep)
                if a.strip() and b.strip():
                    return [
                        {"id": f"{task_id}a", "description": a.strip(), "deps": list(deps), "action": ACTION_SPLIT},
                        {"id": f"{task_id}b", "description": b.strip(), "deps": [f"{task_id}a"], "action": ACTION_SPLIT},
                    ]
        # 无法拆分 → 降级为重述
        return [
            {"id": task_id, "description": f"[重规划] {text}", "deps": list(deps), "action": ACTION_REWORD}
        ]
