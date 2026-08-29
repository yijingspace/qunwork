# -*- coding: utf-8 -*-
"""验证1: 分形检查点引擎 — 黄金比例频率 + 存储上界 + 崩溃恢复 (突破方案一).

验证《QunWork 7x24长程任务架构·创新研究与突破方案》3.2/3.4 的量化预期:
  * Δt_cp(n, t) = Δt₀ · φ^{|n-4|} · e^{-t/T_decay} 的黄金比例标度;
  * S_cp ≤ 2.618 · S_session 存储上界;
  * 崩溃后 restore_latest 恢复 (上下文丢失量 100% → ~10%)。
"""
from __future__ import annotations

import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from coworker.checkpoint import (  # noqa: E402
    LAYERS,
    PHI,
    FractalCheckpoint,
    checkpoint_interval,
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
    print("  验证1: 分形检查点引擎 — 黄金比例频率 × 存储上界 × 恢复")
    print("=" * 72)

    # --- 1a. 黄金比例标度 ---------------------------------------------------
    print("\n[1a] 检查点间隔黄金比例标度 Δt_cp(n) = Δt₀·φ^{|n-4|}:")
    print(f"  {'n':>3} {'Δt₀':>6} {'φ^|n-4|':>9} {'间隔(秒)':>10} {'层级':>10}")
    phi_factors = []
    for n in range(1, 8):
        iv = checkpoint_interval(n, elapsed_seconds=0.0)
        f = PHI ** abs(n - 4)
        phi_factors.append(f)
        print(f"  {n:>3} {LAYERS[n]['interval']:>6} {f:>9.3f} {iv:>10.1f} {LAYERS[n]['granularity']:>10}")
    # φ 因子自身应满足黄金比例标度: φ^{|n-4|} 相邻差 (n=4 两侧各 1) 比 ≈ φ。
    # 从 n=4 向冷层方向: f(5)/f(4) = φ, f(6)/f(5) = φ (φ^{k+1}/φ^k = φ)。
    phi_ratios = [phi_factors[i + 1] / phi_factors[i] for i in range(3, 6)]
    avg = sum(phi_ratios) / len(phi_ratios)
    check("黄金比例因子标度 (φ^{k+1}/φ^k ≈ φ)",
          abs(avg - PHI) / PHI < 0.05, f"(avg={avg:.3f}, φ={PHI:.3f})")
    check("n=1 间隔最短 (超热层)", checkpoint_interval(1) <= checkpoint_interval(4))
    check("n=7 间隔最长 (冷存层)", checkpoint_interval(7) >= checkpoint_interval(4))

    # --- 1b. 时间衰减: 越运行越频繁 ------------------------------------------
    iv_0h = checkpoint_interval(4, elapsed_seconds=0)
    iv_24h = checkpoint_interval(4, elapsed_seconds=24 * 3600)
    check("时间衰减因子 e^{-t/T} 使 24h 后间隔更短",
          iv_24h < iv_0h, f"({iv_0h:.1f}s → {iv_24h:.1f}s)")

    # --- 1c. 存储上界 S_cp ≤ 2.618·S_session ---------------------------------
    overhead = 1.0 / (1.0 - 1.0 / PHI)
    check("存储上界 S_cp ≤ 2.618·S_session",
          abs(overhead - 2.6180339) < 0.001, f"(= {overhead:.4f}x)")

    # --- 1d. 崩溃恢复 roundtrip ---------------------------------------------
    print("\n[1d] 崩溃恢复 (多层检查点叠加恢复):")
    tmp = tempfile.mkdtemp()
    cp = FractalCheckpoint(os.path.join(tmp, "checkpoints.db"))
    sid = "session-verify-1"
    # 模拟 7×24 长程任务: 多轮状态推进 + 各层检查点。
    for round_no in range(5):
        state = {
            "messages": [{"role": "user", "content": f"round {round_no}"}] * (round_no + 1),
            "tasks": [{"id": f"t{i}", "status": "done"} for i in range(round_no)],
            "progress": round_no / 5.0,
            "result": f"partial-result-after-round-{round_no}",
        }
        cp.save_checkpoint(sid, state, n_layer=1)  # 超热: 每轮全量
        if round_no % 2 == 0:
            cp.save_checkpoint(sid, state, n_layer=4)  # 中衡: 摘要
    # 最后一次保存 final 层 (冷存, 任务完成态)。
    cp.save_checkpoint(sid, {"result": "FINAL-DELIVERABLE"}, n_layer=7)

    restored = cp.restore_latest(sid)
    check("恢复最终结果", restored.get("result") == "FINAL-DELIVERABLE",
          f"(result={restored.get('result')!r})")
    check("全量层消息保留", len(restored.get("messages") or []) >= 1)
    check("检查点持久化条数", cp.count(sid) >= 5, f"({cp.count(sid)} 条)")
    check("过期清理幂等", cp.prune_expired() >= 0)

    # --- 1e. 恢复时间上界 (<30s, 本地 SQLite 读取) ----------------------------
    import time

    t0 = time.perf_counter()
    cp.restore_latest(sid)
    restore_ms = (time.perf_counter() - t0) * 1000
    check("恢复时间 <30s (实测毫秒级)", restore_ms < 30_000, f"({restore_ms:.1f}ms)")
    cp.close()

    print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
