"""分形降级引擎 (突破方案五) — 多层级优雅降级, 永不完全失败.

依据《QunWork 7x24长程任务架构·创新研究与突破方案》第七节:
  * 七层降级: L1 正常 → L2 降模型 → L3 缩减范围 → L4 同步模式 →
    L5 仅保留检查点 (暂停) → L6 告警+归档;
  * 降级保真度 ``F(L) = φ^{-(L-1)}·F(1)``: L=1 时 100%, L=4 时 ≈23.6%,
    L=7 时 ≈0%;
  * FSCI 存算一体思想: 每个中间结果自动持久化 (存储即状态), 降级时保留已
    完成子任务结果, 重新执行仅重算未完成部分 (增量恢复)。

与 B7 瓶颈对应: 原任务失败 → ``status="error"`` → 等下次 cron 重试,
无部分完成保留。本引擎把失败转成逐级降级链, 每级保留检查点 + 部分结果,
人工介入频率从"每次失败"降到"仅 L5+"。
"""

from __future__ import annotations

import logging
import math
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

PHI = (1.0 + math.sqrt(5.0)) / 2.0

# 六层降级策略表 (L1-L6; 方案表 7.2 共 6 层, 保真度按 φ 标度)
STRATEGIES: dict[int, dict[str, Any]] = {
    1: {"action": "continue", "resource": 1.0, "fidelity": 1.0},
    2: {"action": "downgrade_model", "resource": 0.6, "fidelity": 0.85},
    3: {"action": "narrow_scope", "resource": 0.4, "fidelity": 0.70},
    4: {"action": "sync_mode", "resource": 0.2, "fidelity": 0.50},
    5: {"action": "checkpoint_pause", "resource": 0.05, "fidelity": 0.0},
    6: {"action": "alert_archive", "resource": 0.0, "fidelity": 0.0},
}

MAX_LEVEL = max(STRATEGIES)


def fidelity(level: int) -> float:
    """降级保真度 F(L) = φ^{-(L-1)}。"""
    return PHI ** (-(max(1, int(level)) - 1))


class FractalDegradation:
    """分形降级引擎 — 失败自动降级链。

    用法::

        deg = FractalDegradation(checkpoint_engine=cp, cheaper_model_fn=...)
        decision = await deg.handle_failure(task_id, exc, engine_state)
        # decision = {"action": "retry"|"pause"|"archive", "level": N, ...}

    集成点 (方案 8.2): ``OrchestrationRunStore`` 增加降级记录; 编排循环的
    失败分支调用 ``handle_failure`` 取代直接 ``status="error"``。
    """

    def __init__(
        self,
        checkpoint_engine: Optional[Any] = None,
        *,
        cheaper_model_fn: Optional[Callable[[str], str]] = None,
        extract_core_fn: Optional[Callable[[Any], Any]] = None,
        alert_fn: Optional[Callable[[str, Exception], None]] = None,
        model_tiers: Optional[list[str]] = None,
    ) -> None:
        self.checkpoint = checkpoint_engine
        self.cheaper_model_fn = cheaper_model_fn
        self.extract_core_fn = extract_core_fn
        self.alert_fn = alert_fn
        # 模型阶梯: 从贵到便宜, 降级时逐级下移。
        self.model_tiers = model_tiers or ["gpt-4o", "gpt-4o-mini", "gpt-4.1-nano"]
        self.levels: dict[str, int] = {}  # task_id -> current_level
        self.history: list[dict[str, Any]] = []  # 降级审计轨迹

    def current_level(self, task_id: str) -> int:
        return self.levels.get(task_id, 1)

    def strategy(self, task_id: str) -> dict[str, Any]:
        return STRATEGIES[self.current_level(task_id)]

    async def handle_failure(
        self, task_id: str, error: Exception, engine_state: dict
    ) -> dict[str, Any]:
        """处理任务失败 — 逐级自动降级, 永不完全失败 (直到 L6 归档)。"""
        level = self.current_level(task_id)
        # 1. 每级都先保存检查点 (FSCI: 存储即状态, 部分结果保留)。
        if self.checkpoint is not None:
            try:
                self.checkpoint.save_checkpoint(task_id, engine_state, n_layer=level)
            except Exception:
                logger.exception("checkpoint on degradation failed for %s", task_id)

        strategy = STRATEGIES[level]
        action = strategy["action"]
        logger.warning("degrading %s → L%d (%s): %s", task_id, level, action, error)

        if action == "continue":
            # L1: 无降级 — 直接重试一次 (保留原模型/范围), 升级到 L2。
            decision = {"action": "retry", "level": level}

        elif action == "downgrade_model":
            new_model = self._cheaper_model(engine_state.get("model") or self.model_tiers[0])
            engine_state["model"] = new_model
            decision = {"action": "retry", "level": level, "model": new_model}

        elif action == "narrow_scope":
            core = self._extract_core(engine_state.get("tasks"))
            engine_state["tasks"] = core
            decision = {"action": "retry", "level": level, "core_tasks": len(core)}

        elif action == "sync_mode":
            # L4: 转为等待人工的同步模式 — 挂起并留痕, 不自动重试。
            decision = {
                "action": "pause",
                "level": level,
                "reason": "degraded to sync mode — human decision required",
            }

        elif action == "checkpoint_pause":
            latest = None
            if self.checkpoint is not None:
                try:
                    latest = self.checkpoint.latest(task_id)
                except Exception:
                    latest = None
            decision = {"action": "pause", "level": level, "checkpoint": latest}

        else:  # alert_archive
            if self.alert_fn is not None:
                try:
                    self.alert_fn(task_id, error)
                except Exception:
                    logger.exception("alert callback failed for %s", task_id)
            decision = {"action": "archive", "level": level}

        # 记录降级轨迹 (审计)。
        self.history.append(
            {
                "task_id": task_id,
                "level": level,
                "action": action,
                "error": str(error),
                "fidelity": strategy["fidelity"],
            }
        )
        # 自动升级降级级别: 除 L6 归档 (终态) 外, 每级降级后进入下一级,
        # 保证链可走到 L6 — "永不完全失败" (每次失败都保留部分结果与检查点)。
        if action != "alert_archive":
            self.levels[task_id] = min(MAX_LEVEL, level + 1)
        return decision

    # -- 策略原语 (可注入) ----------------------------------------------------
    def _cheaper_model(self, model: str) -> str:
        if self.cheaper_model_fn is not None:
            return self.cheaper_model_fn(model)
        if model in self.model_tiers:
            idx = self.model_tiers.index(model)
            return self.model_tiers[min(len(self.model_tiers) - 1, idx + 1)]
        return self.model_tiers[-1]

    def _extract_core(self, tasks: Any) -> Any:
        if self.extract_core_fn is not None:
            return self.extract_core_fn(tasks)
        # 默认: 只保留标记 core / 前 50% 任务 (纯启发式)。
        if isinstance(tasks, list):
            core = [t for t in tasks if isinstance(t, dict) and t.get("core")]
            if core:
                return core
            half = max(1, len(tasks) // 2)
            return tasks[:half]
        return tasks

    def reset(self, task_id: str) -> None:
        """任务成功后重置降级级别。"""
        self.levels.pop(task_id, None)
