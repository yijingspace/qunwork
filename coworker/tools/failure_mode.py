"""S8 工具失败模式库与重试/熔断策略 (蜂群审计报告 G9).

蜂群/主会话执行任务时, 某些工具会反复失败 (网络错误、权限不足、配置缺失、
临时故障)。旧行为: 每次失败模型都重试, 浪费 token 且可能死循环 (G9 短板:
重复失败减少, 稳定性提升)。

本模块提供:
  * FailureModeRegistry (进程内): 记录每个工具最近 N 次调用的失败率;
  * 熔断: 当某工具连续失败 >= max_failures (默认 3) 时进入 open 状态 —
    后续调用在引擎层直接短路返回"工具熔断"错误 (不执行), 并提示模型
    换方案; 冷却期 (cooldown_seconds, 默认 30s) 后自动恢复 half-open
    (允许一次试探);
  * 失败模式库: 记录 (工具, 错误类型) → 失败次数, 供审计/可视化
    (S6 失败模式库复用) 与 Refine 蒸馏 (蜂群任务中哪些工具总失败)。

线程安全 (worker 引擎在多线程执行), 幂等, 无外部依赖。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DEFAULT_MAX_FAILURES = 3
_DEFAULT_COOLDOWN_SECONDS = 30.0
# 记录窗口: 只统计最近 10 次调用 (防长期历史掩盖近期故障)
_WINDOW = 10


class ToolFailureRecord:
    __slots__ = ("tool", "error_type", "failures", "last_failed_at", "circuit_open", "opened_at")

    def __init__(self, tool: str) -> None:
        self.tool = tool
        self.error_type = ""
        self.failures = 0
        self.last_failed_at = 0.0
        self.circuit_open = False
        self.opened_at = 0.0


class FailureModeRegistry:
    """进程内工具失败模式库 + 熔断器。"""

    def __init__(
        self,
        *,
        max_failures: int = _DEFAULT_MAX_FAILURES,
        cooldown_seconds: float = _DEFAULT_COOLDOWN_SECONDS,
    ) -> None:
        self.max_failures = max(1, max_failures)
        self.cooldown = max(1.0, cooldown_seconds)
        self._lock = threading.RLock()
        self._records: dict[str, ToolFailureRecord] = {}
        # 失败模式库: (tool, error_type) -> count (审计/蒸馏用)
        self._mode_counts: dict[tuple[str, str], int] = defaultdict(int)
        # 最近调用历史 (熔断状态判定)
        self._history: dict[str, deque] = defaultdict(lambda: deque(maxlen=_WINDOW))

    # -- 调用记录 -----------------------------------------------------------
    def record_success(self, tool: str) -> None:
        with self._lock:
            rec = self._records.setdefault(tool, ToolFailureRecord(tool))
            rec.failures = max(0, rec.failures - 1)  # 成功衰减失败计数
            if rec.circuit_open:
                # half-open 试探成功 → 关闭熔断
                rec.circuit_open = False
                rec.opened_at = 0.0
                logger.info("tool %r circuit recovered (half-open probe succeeded)", tool)
            self._history[tool].append(True)

    def record_failure(self, tool: str, error_type: str = "") -> None:
        with self._lock:
            rec = self._records.setdefault(tool, ToolFailureRecord(tool))
            rec.error_type = error_type or rec.error_type
            rec.failures += 1
            rec.last_failed_at = time.time()
            self._history[tool].append(False)
            self._mode_counts[(tool, error_type or "unknown")] += 1
            if rec.failures >= self.max_failures and not rec.circuit_open:
                rec.circuit_open = True
                rec.opened_at = time.time()
                logger.warning(
                    "tool %r circuit OPEN after %d consecutive failures (%s)",
                    tool,
                    rec.failures,
                    error_type or "unknown",
                )

    # -- 熔断判定 -----------------------------------------------------------
    def check(self, tool: str) -> Optional[str]:
        """调用前检查: 熔断 open 且未过冷却 → 返回拒绝原因; 否则 None (放行)。
        冷却期后自动转为 half-open (放行一次试探)。"""
        with self._lock:
            rec = self._records.get(tool)
            if rec is None or not rec.circuit_open:
                return None
            if time.time() - rec.opened_at >= self.cooldown:
                # 冷却结束 → half-open: 放行一次 (record_success/failure 决定恢复/再开)
                rec.circuit_open = False
                rec.opened_at = 0.0
                logger.info("tool %r circuit half-open (cooldown elapsed)", tool)
                return None
            return (
                f"tool '{tool}' is circuit-open (failed {rec.failures} times, "
                f"last error: {rec.error_type or 'unknown'}) — do not call it again "
                f"for ~{int(self.cooldown - (time.time() - rec.opened_at))}s; "
                f"use an alternative approach."
            )

    # -- 查询/审计 ----------------------------------------------------------
    def status(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "tool": rec.tool,
                    "failures": rec.failures,
                    "error_type": rec.error_type,
                    "circuit_open": rec.circuit_open,
                    "opened_at": rec.opened_at,
                }
                for rec in sorted(self._records.values(), key=lambda r: -r.failures)
            ]

    def failure_modes(self) -> list[dict[str, Any]]:
        """失败模式库: (tool, error_type) → 失败次数, 供 S6/Refine 复用。"""
        with self._lock:
            return [
                {"tool": t, "error_type": e, "count": c}
                for (t, e), c in sorted(
                    self._mode_counts.items(), key=lambda kv: -kv[1]
                )
            ]

    def most_failing(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._lock:
            return sorted(
                (
                    {"tool": t, "failures": r.failures, "error_type": r.error_type}
                    for t, r in self._records.items()
                ),
                key=lambda d: -d["failures"],
            )[:limit]


# 进程级单例: 所有 engine 共享 (蜂群 worker + 主会话), 跨任务一致性。
_default_registry: Optional[FailureModeRegistry] = None
_registry_lock = threading.Lock()


def get_failure_registry() -> FailureModeRegistry:
    global _default_registry
    with _registry_lock:
        if _default_registry is None:
            _default_registry = FailureModeRegistry()
        return _default_registry
