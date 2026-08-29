# -*- coding: utf-8 -*-
"""验证5: 分形降级引擎 — 降级层级 φ 标度 + 部分结果保留 (突破方案五).

验证《QunWork 7x24长程任务架构·创新研究与突破方案》7.2/7.4 的量化预期:
  * 降级保真度 F(L) = φ^{-(L-1)}: L=1 100%, L=4 ≈23.6%, L=7 ≈0%;
  * L1→L6 逐级降级链, 永不完全失败 (每级保留检查点);
  * 部分完成保留: 降级时已完成的子任务结果保留;
  * 人工介入频率: 仅 L5+ (sync_mode 之后)。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from coworker.checkpoint import FractalCheckpoint  # noqa: E402
from coworker.degradation import (  # noqa: E402
    MAX_LEVEL,
    STRATEGIES,
    FractalDegradation,
    fidelity,
)

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
    print("  验证5: 分形降级引擎 — φ 标度 × 降级链 × 部分保留")
    print("=" * 72)

    # --- 5a. φ 标度保真度 -------------------------------------------------------
    print("\n[5a] 降级保真度 F(L) = φ^{-(L-1)}:")
    prev = 1.0
    for L in range(1, MAX_LEVEL + 1):
        f = fidelity(L)
        ratio = f / prev if prev else 0
        print(f"  L{L}: F = {f:.4f} ({f:.1%}), 相邻比 = {ratio:.4f} (理论 1/φ = {1/1.6180339887:.4f})")
        prev = f
    check("L=1 保真度 100%", abs(fidelity(1) - 1.0) < 1e-9)
    check("L=4 保真度 ≈ φ⁻³ ≈ 23.6%", abs(fidelity(4) - 0.2361) < 0.01, f"({fidelity(4):.4f})")
    # 相邻层比 ≈ 1/φ
    r = fidelity(3) / fidelity(2)
    check("相邻层保真度比 ≈ 1/φ", abs(r - 1 / 1.6180339887) < 0.01, f"({r:.4f})")

    # --- 5b. 降级链 L1→L6 -------------------------------------------------------
    print("\n[5b] 降级链 (连续失败 6 次 → L1..L6):")
    tmp = tempfile.mkdtemp()
    cp = FractalCheckpoint(os.path.join(tmp, "cp.db"))
    deg = FractalDegradation(checkpoint_engine=cp)
    state = {"model": "gpt-4o", "tasks": [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}]}

    async def run_chain() -> list[dict]:
        outs = []
        for i in range(6):
            outs.append(await deg.handle_failure("task-x", RuntimeError(f"failure-{i}"), dict(state)))
            state["model"] = outs[-1].get("model", state["model"])
        return outs

    outs = asyncio.run(run_chain())
    chain = " -> ".join("L%d:%s" % (o["level"], o["action"]) for o in outs)
    print(f"  链: {chain}")
    levels = [o["level"] for o in outs]
    check("降级链逐级 L1→L6", levels == [1, 2, 3, 4, 5, 6], f"({levels})")
    check("L2 动作 = 降模型", outs[1]["action"] == "retry" and "model" in outs[1])
    check("L3 动作 = 缩减范围", outs[2]["action"] == "retry" and "core_tasks" in outs[2])
    check("L4 转同步模式 (人工)", outs[3]["action"] == "pause")
    check("L6 终态 = 告警+归档", outs[5]["action"] == "archive")
    check("每次降级均记录审计轨迹", len(deg.history) == 6)

    # --- 5c. 部分完成保留 (检查点在每级保存) --------------------------------------
    print("\n[5c] 部分完成保留 (FSCI: 存储即状态):")
    cp_count = cp.count("task-x")
    check("每级降级保存检查点 (≥6 条)", cp_count >= 6, f"({cp_count} 条)")
    latest = cp.latest("task-x")
    check("最新检查点可读 (供恢复)", latest is not None and latest["n_layer"] in range(1, MAX_LEVEL + 1),
          f"(n_layer={latest['n_layer'] if latest else None}, granularity={latest['granularity'] if latest else None})")
    restored = cp.restore_latest("task-x")
    check("恢复可得到降级前状态", restored is not None)

    # --- 5d. 降级后重试模型递减 --------------------------------------------------
    print("\n[5d] 降级模型阶梯 (gpt-4o → mini → nano):")
    deg2 = FractalDegradation(checkpoint_engine=None)
    m1 = deg2._cheaper_model("gpt-4o")
    m2 = deg2._cheaper_model(m1)
    print(f"  {m1} → {m2}")
    check("模型逐级降档", m1 == "gpt-4o-mini" and m2 == "gpt-4.1-nano")

    # --- 5e. 范围缩减 (仅保留核心) ------------------------------------------------
    print("\n[5e] 缩减任务范围 (narrow_scope):")
    tasks = [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}]
    core = deg2._extract_core(tasks)
    print(f"  4 任务 → {len(core)} 任务")
    check("范围缩减保留前一半", len(core) == 2)

    # --- 5f. 成功后重置 ------------------------------------------------------------
    print("\n[5f] 任务成功后重置降级级别:")
    deg2.reset("task-x")
    check("重置后回到 L1", deg2.current_level("task-x") == 1)

    print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
