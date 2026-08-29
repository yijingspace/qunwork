"""LoopCoop 收敛引擎 — 任务编排的自动重规划与收敛保证 (突破方案二).

依据《QunWork 7x24长程任务架构·创新研究与突破方案》第四节:
  * 三层编排 (规划器=意图层 / 执行器=运行时层 / 评审器=审计层) 建模为 OIR 耦合矩阵 W;
  * 黄金比例权重 ``[1/φ, 1/φ², 1/φ³]`` → ``|λ₂| = 0.5712``, 约 9 轮收敛到 99%
    (Perron-Frobenius: ``|λ₂| < 1`` 时循环必然收敛, 收敛率 ``|λ₂|^k``);
  * 收敛度检测: ``convergence = (completed + accepted) / (2 * total)``;
  * 超过停滞轮数 (stall_rounds) → 自动重规划而非无限空转。

与 B5/B7 瓶颈对应: 原编排固定 3 轮迭代, 无跨会话持久化中间产物, 失败即 error。
本模块提供: 谱隙计算、收敛度曲线、停滞判定、自动重规划决策 — 供
``Orchestrator.run`` 的迭代循环消费 (见 integration)。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

PHI = (1.0 + math.sqrt(5.0)) / 2.0

# 黄金比例耦合矩阵 (配置6, 经 verify_loopcoop_tuning 验证):
#   W[0][1] = 1/φ     规划→执行传递率
#   W[1][0] = 1/φ²    执行→评审传递率
#   W[1][2] = 1/φ³    执行→评审反馈
#   W[2][1] = 1/φ²    评审→执行重做率
GOLDEN_COUPLING = [
    [0.0, 1.0 / PHI, 0.0],
    [1.0 / PHI**2, 0.0, 1.0 / PHI**3],
    [0.0, 1.0 / PHI**2, 0.0],
]

CONVERGENCE_THRESHOLD = 0.99  # 收敛判定阈值
DEFAULT_MAX_ITERATIONS = 20  # 硬上限 (防止无限循环)
DEFAULT_STALL_ROUNDS = 2  # 连续无进展轮数 → 判定停滞


def spectral_gap(W: list[list[float]]) -> float:
    """耦合矩阵的谱隙: 第二大特征值模长 ``|λ₂|``。

    收敛率 = ``|λ₂|^k``; ``|λ₂| < 1`` 时循环必然收敛 (Perron-Frobenius)。
    无 numpy 时退化为幂迭代近似。
    """
    try:
        import numpy as np

        eigvals = np.linalg.eigvals(np.asarray(W, dtype=float))
        # 特征值可能是复数 — 用模长 (abs 返回 np.float64/complex 的模) 排序。
        abs_eigs = sorted(abs(e) for e in eigvals)
        return float(abs_eigs[-2]) if len(abs_eigs) >= 2 else float(abs_eigs[0])
    except ImportError:
        return _power_iteration_gap(W)


def _power_iteration_gap(W: list[list[float]]) -> float:
    """无 numpy 兜底: 用幂迭代求最大特征值, 再求 W - λ₁I 的最大特征值 (近似 λ₂)。"""
    n = len(W)
    if n == 0:
        return 1.0
    v = [1.0] * n
    for _ in range(200):
        nv = [sum(W[i][j] * v[j] for j in range(n)) for i in range(n)]
        norm = math.sqrt(sum(x * x for x in nv)) or 1.0
        v = [x / norm for x in nv]
    lam1 = sum(v[i] * sum(W[i][j] * v[j] for j in range(n)) for i in range(n))
    W2 = [[W[i][j] - (lam1 if i == j else 0.0) for j in range(n)] for i in range(n)]
    v2 = [1.0] * n
    for _ in range(200):
        nv = [sum(W2[i][j] * v2[j] for j in range(n)) for i in range(n)]
        norm = math.sqrt(sum(x * x for x in nv)) or 1.0
        v2 = [x / norm for x in nv]
    lam2 = sum(v2[i] * sum(W2[i][j] * v2[j] for j in range(n)) for i in range(n))
    return abs(lam2)


def iterations_to_converge(threshold: float = CONVERGENCE_THRESHOLD, W: Optional[list[list[float]]] = None) -> int:
    """收敛到指定阈值所需迭代轮数 ``k = ceil(ln(1-t)/ln(|λ₂|))``。"""
    gap = spectral_gap(W if W is not None else GOLDEN_COUPLING)
    if gap >= 1.0:
        return DEFAULT_MAX_ITERATIONS
    if gap <= 0.0:
        return 1
    return max(1, math.ceil(math.log(1.0 - threshold) / math.log(gap)))


@dataclass
class LoopCoopMonitor:
    """编排收敛监控器: 记录每轮收敛度曲线, 判定停滞, 给出重规划建议。

    用法::

        monitor = LoopCoopMonitor(W=GOLDEN_COUPLING)
        for round in run_loop():
            monitor.record_round(plan, verdicts)
            if monitor.is_converged(): break
            if monitor.is_stalled(): replan = replanner.suggest(plan, monitor.history)
    """

    W: list[list[float]] = field(default_factory=lambda: [list(r) for r in GOLDEN_COUPLING])
    threshold: float = CONVERGENCE_THRESHOLD
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    stall_rounds: int = DEFAULT_STALL_ROUNDS
    history: list[float] = field(default_factory=list)
    iterations: int = 0

    @property
    def gap(self) -> float:
        return spectral_gap(self.W)

    @property
    def theoretical_rounds(self) -> int:
        return iterations_to_converge(self.threshold, self.W)

    def record_round(self, plan: Any, verdicts: Optional[list[dict]] = None) -> float:
        """记录一轮的收敛度 (0..1)。plan 需有 tasks (带 status/result 属性或 dict)。"""
        total = len(plan.tasks) if hasattr(plan, "tasks") else 0
        done = sum(1 for t in plan.tasks if t.done) if hasattr(plan, "tasks") else 0
        accepted = 0
        for v in verdicts or []:
            if isinstance(v, dict):
                if v.get("accepted"):
                    accepted += 1
            else:
                acc = getattr(v, "accepted", None)
                if acc:
                    accepted += 1
        conv = (done + accepted) / (2 * total) if total else 0.0
        self.history.append(min(1.0, max(0.0, conv)))
        self.iterations += 1
        return self.history[-1]

    def is_converged(self) -> bool:
        """最近一轮收敛度 ≥ 阈值。"""
        return bool(self.history) and self.history[-1] >= self.threshold

    def is_stalled(self) -> bool:
        """连续 stall_rounds 轮收敛度没有提升 (固定点检测)。"""
        if len(self.history) < self.stall_rounds + 1:
            return False
        recent = self.history[-self.stall_rounds:]
        return max(recent) <= self.history[-self.stall_rounds - 1]

    def hit_max_iterations(self) -> bool:
        return self.iterations >= self.max_iterations

    def report(self) -> dict[str, Any]:
        return {
            "gap": self.gap,
            "theoretical_rounds": self.theoretical_rounds,
            "iterations": self.iterations,
            "convergence_curve": list(self.history),
            "final_convergence": self.history[-1] if self.history else 0.0,
            "converged": self.is_converged(),
            "stalled": self.is_stalled(),
        }
