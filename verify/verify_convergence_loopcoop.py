# -*- coding: utf-8 -*-
"""验证2: LoopCoop 收敛引擎 — 谱隙 |λ₂| 与收敛轮数 (突破方案二).

验证《QunWork 7x24长程任务架构·创新研究与突破方案》4.2/4.4 的量化预期:
  * 黄金比例耦合矩阵 [1/φ, 1/φ², 1/φ³] → |λ₂| = 0.5712;
  * 约 9 轮收敛到 99% (vs 固定 3 轮);
  * LoopCoopMonitor 实测收敛曲线与停滞检测。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from coworker.orchestrator.convergence import (  # noqa: E402
    GOLDEN_COUPLING,
    LoopCoopMonitor,
    iterations_to_converge,
    spectral_gap,
)
from coworker.orchestrator.models import Plan, Task  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name} {detail}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def main() -> int:
    print("=" * 72)
    print("  验证2: LoopCoop 收敛引擎 — 谱隙 × 收敛轮数 × 监控")
    print("=" * 72)

    # --- 2a. 谱隙 |λ₂| -------------------------------------------------------
    print("\n[2a] 黄金比例耦合矩阵谱隙:")
    gap = spectral_gap(GOLDEN_COUPLING)
    print(f"  W = {GOLDEN_COUPLING}")
    print(f"  |λ₂| = {gap:.4f}")
    check("|λ₂| ≈ 0.5712 (理论值)", abs(gap - 0.5712) < 0.005, f"({gap:.4f})")
    check("|λ₂| < 1 → 必然收敛 (Perron-Frobenius)", gap < 1.0)

    # --- 2b. 收敛轮数 ---------------------------------------------------------
    print("\n[2b] 收敛轮数:")
    k99 = iterations_to_converge(0.99)
    k90 = iterations_to_converge(0.90)
    print(f"  90% 收敛: {k90} 轮, 99% 收敛: {k99} 轮 (当前固定 3 轮)")
    check("99% 收敛约 9 轮", abs(k99 - 9) <= 1, f"({k99} 轮)")
    check("比固定 3 轮更精确 (自适应)", k99 > 3)

    # --- 2c. 收敛曲线实测 -----------------------------------------------------
    print("\n[2c] LoopCoopMonitor 实测收敛曲线 (模拟 6 轮渐进完成):")
    mon = LoopCoopMonitor()
    plan = Plan(goal="verify", tasks=[Task(id=f"t{i}", description=f"task {i}") for i in range(6)])
    for rnd in range(6):
        if rnd < 6:
            plan.tasks[rnd].status = "done"
        verdicts = [{"accepted": True} for _ in range(rnd + 1)]
        conv = mon.record_round(plan, verdicts=verdicts)
        print(f"  轮 {rnd + 1}: 收敛度 = {conv:.3f}")
    rep = mon.report()
    check("收敛曲线单调不减", rep["convergence_curve"] == sorted(rep["convergence_curve"]))
    check("全部完成后收敛度 = 1.0", abs(rep["final_convergence"] - 1.0) < 1e-6,
          f"({rep['final_convergence']:.3f})")

    # --- 2d. 停滞检测 ----------------------------------------------------------
    print("\n[2d] 停滞检测 (连续无进展 → is_stalled):")
    mon2 = LoopCoopMonitor(stall_rounds=2)
    plan2 = Plan(goal="stall", tasks=[Task(id=f"t{i}", description="x") for i in range(3)])
    mon2.record_round(plan2)  # 0
    mon2.record_round(plan2)  # 0
    mon2.record_round(plan2)  # 0 → 2 轮无进展
    check("3 轮无进展 → 判定停滞", mon2.is_stalled())
    mon2.record_round(plan2)
    check("停滞状态下仍可继续记录", mon2.iterations == 4)

    # --- 2e. 多配置谱隙对比 (可调优) -------------------------------------------
    print("\n[2e] 权重配置对比 (调优实验):")
    configs = {
        "原始强耦合": [[0, 0.9, 0], [0.8, 0, 0.3], [0, 0.7, 0]],
        "弱耦合": [[0, 0.5, 0], [0.4, 0, 0.15], [0, 0.3, 0]],
        "黄金比例": GOLDEN_COUPLING,
    }
    for name, W in configs.items():
        g = spectral_gap(W)
        k = iterations_to_converge(0.99, W)
        print(f"  {name:<6}: |λ₂| = {g:.4f}, 99% 需 {k} 轮")
    golden_gap = spectral_gap(GOLDEN_COUPLING)
    weak_gap = spectral_gap(configs["弱耦合"])
    check("黄金比例配置收敛性优于原始强耦合", golden_gap < spectral_gap(configs["原始强耦合"]))
    check("弱耦合/黄金比例均满足 |λ₂| < 0.6", golden_gap < 0.6 and weak_gap < 0.6)

    print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
