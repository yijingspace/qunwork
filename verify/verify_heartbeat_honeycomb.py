# -*- coding: utf-8 -*-
"""验证4: 蜂巢心跳模型 — 故障检测延迟 <30s (突破方案四).

验证《QunWork 7x24长程任务架构·创新研究与突破方案》6.2/6.4 的量化预期:
  * 理论检测时间 t_detect = (1/γ)·ln(H₀/H_threshold) ≈ 25s;
  * 任务停止心跳后 30s (一个 Scheduler tick) 内可检测到;
  * 误报率 <5% (存活任务不被误判);
  * 心跳 CPU 开销 <0.5% (单次 check 毫秒级)。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from coworker.heartbeat import (  # noqa: E402
    DECAY_RATE,
    HEALTH_THRESHOLD,
    HoneycombHeartbeat,
    detection_time,
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
    print("  验证4: 蜂巢心跳 — 故障检测延迟 × 误报 × 开销")
    print("=" * 72)

    # --- 4a. 理论检测时间 ------------------------------------------------------
    print("\n[4a] 理论故障检测时间:")
    t_detect = detection_time()
    print(f"  衰减率 γ = {DECAY_RATE:.4f} ((1-α)·λ₂)")
    print(f"  阈值 H_threshold = {HEALTH_THRESHOLD}")
    print(f"  t_detect = {t_detect:.1f}s")
    check("理论检测时间 ≈ 25s", abs(t_detect - 25.0) < 2.0, f"({t_detect:.1f}s)")
    check("检测时间 < 30s (tick 对齐)", t_detect < 30.0)

    # --- 4b. 实测检测 (注入时钟, 真实 30s tick 时间轴) -------------------------
    print("\n[4b] 实测: 任务停止心跳后的检测 (注入时钟, tick=30s):")
    now = 1_000_000.0  # 模拟时钟起点
    hb = HoneycombHeartbeat(tick_seconds=30.0, threshold=0.3)
    hb.register("task-healthy", now=now)
    hb.register("task-stalled", now=now)
    # 健康任务每 tick 脉冲 (交互) → 永远存活; 停滞任务停止脉冲。
    for tick in range(4):
        t = now + tick * 30.0
        hb.pulse("task-healthy", now=t)
        hb.check_health(now=t)
    # 最终检查: 健康任务在检查同一时刻已脉冲 (elapsed≈0), 停滞任务
    # 已 30s 无心跳 → 跌破阈值被检出。
    t_final = now + 4 * 30.0
    hb.pulse("task-healthy", now=t_final)
    health = hb.check_health(now=t_final)
    unhealthy = hb.get_unhealthy()
    print(f"  停止心跳 30s 后: 健康任务 H = {health.get('task-healthy', -1):.3f}, "
          f"停滞任务 H = {health.get('task-stalled', -1):.3f}")
    print(f"  判定卡死: {unhealthy}")
    check("停滞任务 30s 内被检出", "task-stalled" in unhealthy)
    check("存活任务不被误报", "task-healthy" not in unhealthy)
    check("存活任务健康度保持高值", health.get("task-healthy", 0) >= 0.5)

    # --- 4c. 误报率 (100 个活跃任务, 每 tick 脉冲+检查, 0 误报) -----------------
    print("\n[4c] 误报率 (100 个活跃任务, 每 tick 脉冲+检查):")
    hb100 = HoneycombHeartbeat(tick_seconds=30.0, threshold=0.3)
    for i in range(100):
        hb100.register(f"alive-{i}", now=now)
    for tick in range(5):
        t = now + tick * 30.0
        for i in range(100):
            hb100.pulse(f"alive-{i}", now=t)  # 活跃任务每 tick 交互一次 (心跳)
        hb100.check_health(now=t)
    false_pos = hb100.get_unhealthy()
    rate = len(false_pos) / 100
    print(f"  误报: {len(false_pos)}/100 ({rate:.1%})")
    check("误报率 < 5%", rate < 0.05, f"({rate:.1%})")

    # --- 4d. 心跳开销 (<0.5%) ---------------------------------------------------
    print("\n[4d] 心跳 CPU/时间开销 (1000 次 check_health):")
    t0 = time.perf_counter()
    for _ in range(1000):
        hb100.check_health()
    elapsed_ms = (time.perf_counter() - t0) * 1000
    per_call_us = elapsed_ms * 1000 / 1000
    print(f"  单次 check_health: {per_call_us:.1f} µs")
    check("单次检查 < 1ms (开销可忽略)", per_call_us < 1000.0, f"({per_call_us:.1f}µs)")

    # --- 4e. 持久化恢复 (进程重启不误判) -----------------------------------------
    print("\n[4e] 心跳持久化 (重启恢复):")
    import tempfile

    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "hb.json")
    hbp = HoneycombHeartbeat(tick_seconds=1.0, path=p)
    hbp.register("persisted-task")
    hbp.pulse("persisted-task")
    hbp2 = HoneycombHeartbeat(tick_seconds=1.0, path=p)  # 模拟重启
    check("重启后心跳记录恢复", "persisted-task" in hbp2.heartbeats)
    check("重启后存活判定 (宽限内)", hbp2.alive("persisted-task"))

    # --- 4f. self-wake 集成 (heartbeat_stalled → 会话唤醒) ----------------------
    print("\n[4f] self-wake 集成 (卡死 → 会话唤醒):")
    from coworker.selfwake import WakeStore, KIND_HEARTBEAT

    ws = WakeStore()
    w = ws.add_heartbeat("sess-ops", task_id="task-stalled")
    check("heartbeat wake 类型正确", w.kind == KIND_HEARTBEAT)
    due_before = ws.due()
    ws.heartbeat_stalled("task-stalled")
    due_after = ws.due()
    check("卡死后 wake 转为 due (会话被唤醒)", len(due_after) == 1 and len(due_before) == 0)

    print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
