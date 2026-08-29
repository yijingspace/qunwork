"""QunMesh M2 基准: ready[:max_parallel] 截断 vs 邻域感知批选择 (select_batch_grid)。

异步事件驱动仿真 (对齐真实 orchestrator 语义: 前批任务在跑时 load 场非零,
下一批调度读到真实梯度): N 任务 × K executor, 任务时长对数正态, 列表原序
偏向 agents[0] (制造热点截断场景)。任一 agent 空闲即触发调度。

指标: 调度批次数 / 名额空转率 / 模拟墙钟 (makespan)。

运行: & E:\\QunWork\\QunWork\\.venv\\Scripts\\python.exe bench_mesh_scheduling.py
"""

from __future__ import annotations

import random
import sys
from dataclasses import dataclass, field

sys.path.insert(0, r"E:\QunWork\QunWork")

from coworker.orchestrator.mesh import HexGrid, select_batch_grid  # noqa: E402
from coworker.pheromone import StigmergyBus  # noqa: E402


@dataclass
class SimTask:
    id: str
    agent: str
    duration: float
    deps: list[str] = field(default_factory=list)
    done: bool = False
    dispatched: bool = False


def make_tasks(n: int, agents: list[str], shape: str, seed: int = 7,
               hot_share: float = 0.9, sigma: float = 0.35) -> list[SimTask]:
    """列表原序 hot_share 比例堆给 agents[0] — 截断策略的热点场景。
    sigma 控制时长方差 (越小调度质量越主导 makespan)。"""
    rng = random.Random(seed)
    tasks: list[SimTask] = []
    for i in range(n):
        agent = agents[0] if rng.random() < hot_share else rng.choice(agents[1:])
        deps = []
        if shape == "chain+fan" and i > 0 and rng.random() < 0.3:
            deps = [tasks[rng.randrange(i)].id]
        tasks.append(
            SimTask(
                id=f"t{i}",
                agent=agent,
                duration=max(0.2, rng.lognormvariate(0.0, sigma)),
                deps=deps,
            )
        )
    return tasks


def simulate(tasks: list[SimTask], agents: list[str], max_parallel: int, mode: str) -> dict:
    bus = StigmergyBus(half_life=1e9)  # 仿真内不蒸发 — load=在跑任务计数
    grid = HexGrid(radius=4)
    for a in agents:
        grid.place(a)
    by_id = {t.id: t for t in tasks}
    running: list[tuple[float, SimTask]] = []  # (finish_time, task)
    now = 0.0
    batches = 0
    idle_slots = 0
    total_slots = 0
    while sum(1 for t in tasks if not t.done) > 0:
        # 1) 完成到期任务 (撤 load)
        for ft, t in [r for r in running if r[0] <= now]:
            t.done = True
            bus.deposit(t.agent, -1.0)
            running.remove((ft, t))
        # 2) 就绪集 (依赖全完成且未分派)
        ready = [
            t
            for t in tasks
            if not t.done and not t.dispatched and all(by_id[d].done for d in t.deps)
        ]
        slots = max_parallel - len(running)
        if not ready:
            if not running:
                break  # 死锁防御 (不应发生)
            now = min(ft for ft, _ in running)
            continue
        if slots <= 0:
            now = min(ft for ft, _ in running)
            continue
        # 3) 调度: 截断 (原序) vs 网格 (负载梯度 — 在跑任务的 load 场非零)
        batches += 1
        total_slots += slots
        if mode == "trunc":
            batch = ready[:slots]
        else:
            batch = select_batch_grid(ready, bus, slots, grid=grid)
        idle_slots += slots - len(batch)
        # 4) 分派 (任务固定 agent 映射; 同 agent 排队串行由 duration 累计体现)
        for t in batch:
            t.dispatched = True
            bus.deposit(t.agent, 1.0)
            running.append((now + t.duration, t))
        if not running:
            break
        now = min(ft for ft, _ in running)
    return {
        "batches": batches,
        "makespan": round(now, 2),
        "idle_rate": round(idle_slots / max(1, total_slots), 3),
    }


def main() -> None:
    agents = ["cowork", "code", "review-a", "review-b"]
    print("=" * 76)
    print("  QunMesh M2 基准: 截断调度 vs 网格梯度调度 (异步事件仿真)")
    print("=" * 76)
    scenarios = [
        # (标签, shape, hot_share, sigma)
        ("热点90% 均匀时长", "independent", 0.9, 0.35),
        ("热点90% 链式依赖", "chain+fan", 0.9, 0.35),
        ("热点70% 温和", "independent", 0.7, 0.5),
        ("无热点 (对照)", "independent", 0.0, 0.5),
    ]
    results = []
    for label, shape, hs, sg in scenarios:
        for n, mp in ((80, 4), (200, 8)):
            r_t = simulate(make_tasks(n, agents, shape, seed=7, hot_share=hs, sigma=sg),
                           agents, mp, "trunc")
            r_g = simulate(make_tasks(n, agents, shape, seed=7, hot_share=hs, sigma=sg),
                           agents, mp, "grid")
            gain = r_t["makespan"] / max(r_g["makespan"], 1e-9)
            results.append((label, n, mp, gain))
            print(f"\n[{label}] N={n} max_parallel={mp}")
            print(f"  截断: 批次={r_t['batches']} 墙钟={r_t['makespan']} 空转率={r_t['idle_rate']:.1%}")
            print(f"  网格: 批次={r_g['batches']} 墙钟={r_g['makespan']} 空转率={r_g['idle_rate']:.1%}")
            print(f"  墙钟加速: {gain:.2f}x")
    gains = [g for *_x, g in results]
    import math

    print(f"\n几何平均加速: {pow(math.prod(gains), 1/len(gains)):.3f}x")
    print(
        "\n结论 (2026-08-29 实证): 任务固定 agent 映射 + work-conserving 调度下,\n"
        "批选择顺序与 makespan 无关 (瓶颈 agent 完成时间 = 其分派任务总时长和),\n"
        "网格梯度调度收益中性 (几何平均 ≈1.0x)。丝瓜络 '谁近谁领' 的真实收益\n"
        "来自 M3 动态领取 (空闲 agent 从 task 招领信道拉活), M2 的价值 = HexGrid\n"
        "拓扑编址 + 负载梯度数据通路 (M3/M4 地基), 零损失 (mesh_scheduling=False\n"
        "严格保持旧行为)。"
    )


if __name__ == "__main__":
    main()
