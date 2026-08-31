"""QunMesh M5: 节点杀死拾取率压测 — 真实调度层故障注入 (harness 心跳联动)。

区别于 bench_node_failure.py 的离散事件仿真, 本脚本驱动**真 Orchestrator 调度
全链路** (批选择 → 弹性供给 → 事件心跳 → reap 巡检 → inflight 取消 → 任务回池
→ 动态拾取), 仅把 LLM 引擎替换为可控 stub:

场景: N 任务 × K 并发运行中, 30% 任务完成后杀死 1 个在路实例 —
  模拟进程僵死语义: 实例心跳静默 (last_heartbeat 拨回 reap_timeout 之前)
  + 其当前协程永久挂死 (frozen, 模拟 worker 进程死锁)。

两种语义对照:
  dynamic (M5 新行为): reap_interval>0 → reap_stale 判 FAULT → 取消 inflight
      协程 → 任务回池 (retries 不增) → 弹性供给补新节点 → 幸存者拾取。
  legacy (回滚开关):   reap_interval=None → 无 reap 巡检, frozen 任务挂死,
      调度轮 gather 无进展 → stall (T4) → 由外层强杀兜底。

指标: 总完成率 / victim 任务拾取率 / makespan / node_fault 事件数。
对齐研究方案 M4 验证指标「节点杀死后任务拾取率 100%」。

运行: & E:\\QunWork\\QunWork\\.venv\\Scripts\\python.exe scripts\\bench_node_kill_live.py
"""

from __future__ import annotations

import asyncio
import os
import random
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_TASK_ID_RE = re.compile(r"^Task \[(t\d+)\]")

import coworker.orchestrator.orchestrator as orch_mod
from coworker.orchestrator.models import Plan, ReviewVerdict, Task
from coworker.orchestrator.orchestrator import Orchestrator
from coworker.team.agent_pool import AgentPool, AgentState

N_TASKS = 12
K_PARALLEL = 4
KILL_AFTER = 0.30          # 30% 任务完成后注入故障
N_VICTIMS = 1              # 杀 1 个在路实例
REAP_INTERVAL = 0.15       # dynamic: reap 巡检周期 (秒)
REAP_TIMEOUT = 0.60        # 心跳静默超时 → FAULT
RUN_CAP = 45.0             # 外层强杀兜底 (legacy frozen 挂死时)


def make_orch(mode: str, dur: dict, state: dict) -> tuple[Orchestrator, list]:
    events: list[tuple[str, dict]] = []
    rng = random.Random(11)
    tasks = [
        Task(
            id=f"t{i}",
            description=f"章节任务 t{i}: 独立子报告约 200 字 (kill-live 压测)",
        )
        for i in range(N_TASKS)
    ]
    pool = AgentPool() if mode == "dynamic" else AgentPool()

    DELIVERABLE = (
        "## 交付正文\n本章节完成系统设计与实测数据分析：输入经四信道信息素总线分发，"
        "负载信号驱动邻域批选择，任务按网格坐标就近领取；热点检测窗口触发梯度迁徙，"
        "评审阶段由邻域节点三票聚合通过。整体链路延迟与吞吐满足预期阈值，"
        "附录含完整参数表与复现命令行，可供后续回归与压测直接引用。"
    )

    async def fake_run_engine(engine, prompt, on_event=None):
        try:
            # 锚定 prompt 头部 "Task [tN]:" — hints/deps 文本可能包含其它任务
            # 描述原文 (含 "tK:" 字样), 子串扫描会误提取 → 任务混淆
            m = _TASK_ID_RE.match(prompt)
            tid = m.group(1) if m else None
        except Exception:
            import traceback; traceback.print_exc(); raise
        if tid is None:
            # 追问轮 (_looks_like_interim 触发): 直接给完整交付, 不进 frozen 循环
            return DELIVERABLE, "completed"
        if os.environ.get("KILL_LIVE_DEBUG"):
            print(f"ENGINE>> tid={tid}")
        if on_event is not None:
            # 事件驱动心跳 (真实语义: 活跃 worker 持续续心跳, 不会被 reap 误杀)
            on_event("tool_thought", {"text": f"working on {tid}"})
        deadline = time.monotonic() + dur[tid]
        last_beat = time.monotonic()
        while time.monotonic() < deadline:
            if tid in state["frozen"]:
                await asyncio.sleep(3600)  # 节点死亡: 协程永久挂死 (心跳随之静默)
            await asyncio.sleep(0.02)
            # 真实 LLM 引擎持续产 thought/tool 事件 → 事件心跳保活
            if on_event is not None and time.monotonic() - last_beat > 0.1:
                on_event("tool_thought", {"text": f"working on {tid}"})
                last_beat = time.monotonic()
        return f"[{tid}] {DELIVERABLE}", "completed"

    def fake_build_engine(task, role_tag=None, *a, **kw):
        return object()  # 不起真 shell 进程

    async def fake_review(task, result):
        return ReviewVerdict(accepted=True, confidence=0.95, reason="stub")

    orch = Orchestrator(
        provider=None,
        model="stub",
        workspace=str(Path(__file__).resolve().parents[1] / ".tmp-kill-live"),
        initial_plan=Plan(goal="kill-live 压测", tasks=tasks),
        max_parallel=K_PARALLEL,
        timeout_seconds=None,
        task_timeout_seconds=None,
        event_sink=lambda kind, payload: events.append((kind, payload)),
        agent_pool=pool,
        mesh_scheduling=True,
        mesh_claim=True,
        stall_rounds_threshold=3,
        # M5 心跳联动: dynamic 开 reap 巡检; legacy 关 (回滚开关语义)。
        # heartbeat_interval=3600 = 泵等效死亡 — 真实进程死时泵与引擎同时停,
        # 压测隔离变量: victim 唯一心跳源是事件心跳, frozen 后即静默 → reap 检测。
        heartbeat_interval=3600.0,
        reap_interval=REAP_INTERVAL if mode == "dynamic" else None,
        reap_timeout=REAP_TIMEOUT,
        refine_auto=False,
    )
    # 实例级/模块级 stub — 真实调度层不变, 仅隔离 LLM 与评审
    orch_mod._run_engine_async = fake_run_engine
    orch._build_executor_engine = fake_build_engine
    orch._review = fake_review
    return orch, events


async def inject_kills(orch: Orchestrator, pool: AgentPool, dur: dict, state: dict) -> None:
    """30% 任务完成后杀死 N_VICTIMS 个在路实例 (心跳静默 + 协程 frozen)。"""
    plan = orch._last_plan
    while plan is None:
        await asyncio.sleep(0.05)
        plan = orch._last_plan
    killed = 0
    while killed < N_VICTIMS:
        await asyncio.sleep(0.05)
        done = sum(1 for t in plan.tasks if t.done)
        if done < int(N_TASKS * KILL_AFTER):
            continue
        working = pool.list(state=AgentState.WORKING)
        # 只杀真正在跑任务的实例 (避开刚 release/尚未领任务的窗口)
        candidates = [
            a for a in working
            if a.current_task_id
            and next((t for t in plan.tasks if t.id == a.current_task_id), None) is not None
            and next(t for t in plan.tasks if t.id == a.current_task_id).status == "running"
        ]
        if not candidates:
            continue
        victim = candidates[0]
        tid = victim.current_task_id
        if not tid:
            continue
        state["frozen"].add(tid)          # 协程永久挂死 (进程僵死语义)
        victim.last_heartbeat -= (REAP_TIMEOUT + 5.0)  # 心跳静默 → reap 判 FAULT
        state["victims"].append((victim.id, tid))
        killed += 1
        print(f"  [kill] victim={victim.id} inflight_task={tid} (心跳拨老 {REAP_TIMEOUT + 5.0:.1f}s + 协程 frozen)")
        if os.environ.get("KILL_LIVE_DEBUG"):
            await asyncio.sleep(0.5)
            manual = pool.reap_stale(timeout=REAP_TIMEOUT)
            v = pool.get(victim.id)
            print(f"  [debug] manual reap -> {manual}; victim state={v.state if v else None}")
    # frozen 自愈: 任务回池 (status 回 pending) 后解除 frozen — 新协程正常执行
    # (节点死了但编排器重派 = 新执行槽位; frozen 只标记"被杀那一刻的旧协程")
    while state["frozen"]:
        await asyncio.sleep(0.02)
        for t in plan.tasks:
            if t.id in state["frozen"] and t.status == "pending":
                state["frozen"].discard(t.id)


async def run_mode(mode: str) -> dict:
    rng = random.Random(11)
    # 任务时长 0.8~1.2s — 保证 30% 完成注入故障时, 第二批在跑任务还有
    # ≥0.6s 剩余寿命 (victim 不至于在注入前自然完成 → frozen 失效)。
    # t4 强制 3.0s 长任务: victim 锁定为它, 注入时刻必然在跑 (deterministic)。
    dur = {f"t{i}": rng.uniform(0.8, 1.2) for i in range(N_TASKS)}
    dur["t4"] = 3.0
    state: dict = {"frozen": set(), "victims": []}
    orch, events = make_orch(mode, dur, state)
    start = time.monotonic()
    run = asyncio.create_task(orch.run("kill-live 压测"))
    killer = asyncio.create_task(inject_kills(orch, orch.agent_pool, dur, state))
    try:
        result = await asyncio.wait_for(run, timeout=RUN_CAP)
        status = getattr(result, "status", "unknown")
    except asyncio.TimeoutError:
        status = f"hard-killed@{RUN_CAP}s"
    finally:
        killer.cancel()
    makespan = time.monotonic() - start
    plan = orch._last_plan
    done = [t.id for t in plan.tasks if t.done]
    victim_ids = {tid for _, tid in state["victims"]}
    victim_done = [tid for tid in victim_ids if tid in done]
    faults = [p for k, p in events if k == "node_fault"]
    requeues = [p for k, p in events if k == "node_fault_requeue"]
    return {
        "mode": mode,
        "status": status,
        "done": len(done),
        "total": N_TASKS,
        "completion": len(done) / N_TASKS,
        "victim_pickup": (len(victim_done) / len(victim_ids)) if victim_ids else 1.0,
        "victim_ids": sorted(victim_ids),
        "victim_done": sorted(victim_done),
        "makespan": makespan,
        "node_fault_events": len(faults),
        "requeue_events": len(requeues),
        "faulted_agents": [p.get("agent") for p in faults],
    }


async def main() -> int:
    print(f"QunMesh M5 节点杀死拾取率压测 (真实调度层, 引擎 stub)\n"
          f"场景: {N_TASKS} 任务 × {K_PARALLEL} 并发, {int(KILL_AFTER * 100)}% 完成后杀 "
          f"{N_VICTIMS} 个在路实例 (心跳静默 {REAP_TIMEOUT}s + 协程 frozen)\n")
    rows = []
    for mode in ("dynamic", "legacy"):
        print(f"--- {mode} ---")
        rows.append(await run_mode(mode))
        await asyncio.sleep(0.1)
    print("\n| 模式 | 结束态 | 完成率 | victim 拾取率 | makespan | node_fault | requeue |")
    print("|---|---|---|---|---|---|---|")
    for r in rows:
        print(
            f"| {r['mode']} | {r['status']} | {r['done']}/{r['total']} "
            f"({r['completion']:.0%}) | {len(r['victim_done'])}/{len(r['victim_ids'])} "
            f"({r['victim_pickup']:.0%}) | {r['makespan']:.1f}s | "
            f"{r['node_fault_events']} | {r['requeue_events']} |"
        )
    dyn, leg = rows[0], rows[1]
    ok = dyn["victim_pickup"] == 1.0 and dyn["completion"] == 1.0
    print(
        f"\n结论: dynamic 拾取率 {dyn['victim_pickup']:.0%} / 完成率 {dyn['completion']:.0%} "
        f"(reap→取消→回池→弹性补节点→幸存者拾取全链路生效); "
        f"legacy 拾取率 {leg['victim_pickup']:.0%} (无 reap 巡检, frozen 任务永久滞留)。"
        f"验证指标「节点杀死后任务拾取率 100%」: {'达成' if ok else '未达成'}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
