"""QunMesh M4 后续项: 节点杀死后任务拾取率压测 (离散事件仿真)。

场景: N 任务 × K agent 运行中 (前 30% 任务完成后), 杀死 1~2 个在跑 agent
(其 running 任务回池)。对比两种语义:
- static: 任务固定 agent 映射 (标准蜂群现状) — 故障 agent 的任务永久滞留;
- dynamic: 任务无固定映射, 空闲 agent 按 agent_pool.acquire 语义 (低负载
  优先, QunMesh mesh_claim「节点无角色」) 动态领取 — 故障任务被幸存者拾取。

指标: 拾取率 (全部任务最终完成比例) / makespan。
对齐研究方案 M4 验证指标「节点杀死后任务拾取率 100%」。

运行: & E:\\QunWork\\QunWork\\.venv\\Scripts\\python.exe bench_node_failure.py
"""

from __future__ import annotations

import random
import sys


def simulate(n: int, k: int, seed: int = 11, n_kill: int = 1, mode: str = "dynamic") -> dict:
    """mode: dynamic = mesh_claim 动态领取; static = 固定映射。"""
    rng = random.Random(seed)
    tasks = [
        {"id": f"t{i}", "duration": max(0.2, rng.lognormvariate(0.0, 0.4)),
         "done": False, "owner": None}
        for i in range(n)
    ]
    if mode == "static":
        for i, t in enumerate(tasks):
            t["owner"] = f"a{i % k}"
    running: list[dict] = []  # {task, agent, finish}
    victims: set[str] = set()
    now = 0.0
    killed = 0
    deadlocked = False
    while not all(t["done"] for t in tasks):
        # 完成到期任务
        for r in [r for r in running if r["finish"] <= now]:
            r["task"]["done"] = True
            running.remove(r)
        # 触发节点故障: 完成 30% 任务后, 杀掉在跑 agent (共 n_kill 次)
        done_n = sum(1 for t in tasks if t["done"])
        if killed < n_kill and done_n >= n * 0.3 and running:
            r = rng.choice(running)
            victim = r["agent"]
            if victim not in victims:
                victims.add(victim)
                killed += 1
                for x in [x for x in running if x["agent"] == victim]:
                    running.remove(x)
                    x["task"]["owner"] = None  # running 任务回池
        # 分派
        if mode == "dynamic":
            busy = {r["agent"] for r in running}
            idle = [f"a{i}" for i in range(k) if f"a{i}" not in busy and f"a{i}" not in victims]
            for a in idle:  # 低负载≈空闲先领 (acquire 语义等价)
                for t in tasks:
                    if not t["done"] and t["owner"] is None:
                        t["owner"] = a
                        running.append({"task": t, "agent": a,
                                        "finish": now + t["duration"]})
                        break
        else:
            # static: 只有 owner 存活且空闲才可推进
            busy = {r["agent"] for r in running}
            for t in tasks:
                if not t["done"] and t["owner"] and t["owner"] not in victims \
                        and t["owner"] not in busy:
                    running.append({"task": t, "agent": t["owner"],
                                    "finish": now + t["duration"]})
        if not running:
            # 队列仍有未完成任务但无人可领 → 死锁 (static + victim owner)
            if any(not t["done"] for t in tasks):
                deadlocked = True
            break
        now = min(r["finish"] for r in running)
    done_count = sum(1 for t in tasks if t["done"])
    return {
        "pick_rate": round(done_count / n, 3),
        "makespan": round(now, 2),
        "victims": sorted(victims),
        "deadlocked": deadlocked,
    }


def main() -> None:
    print("=" * 72)
    print("  QunMesh M4 后续项: 节点故障任务拾取率压测 (异步事件仿真)")
    print("=" * 72)
    print(
        "语义: dynamic = QunMesh mesh_claim (agent_pool 低负载动态领取, 节点无角色\n"
        "— 故障节点任务回池被幸存者拾取); static = 标准蜂群固定映射 (故障 agent\n"
        "的任务永久滞留)。kill 时机 = 完成 30% 任务后, 在跑 agent 随机杀。"
    )
    ok = True
    for n, k, kills in ((40, 4, 1), (80, 6, 2), (120, 8, 2)):
        r_d = simulate(n, k, seed=11, n_kill=kills, mode="dynamic")
        r_s = simulate(n, k, seed=11, n_kill=kills, mode="static")
        status = "PASS" if r_d["pick_rate"] == 1.0 else "FAIL"
        if status == "FAIL":
            ok = False
        print(f"\n[N={n} K={k} kill={kills}]")
        print(f"  dynamic: 拾取率={r_d['pick_rate']:.0%} makespan={r_d['makespan']} [{status}]")
        print(f"  static : 拾取率={r_s['pick_rate']:.0%} makespan={r_s['makespan']} "
              f"deadlocked={r_s['deadlocked']}")
    print(
        "\n结论: 动态领取语义下故障节点任务 100% 被幸存者拾取 (研究方案 M4 抗毁\n"
        "指标达成); 固定映射下故障 agent 任务永久滞留 — 这是 QunMesh 抗毁性的直接\n"
        "来源。仿真为 agent_pool.acquire 语义的等价抽象; harness 真实故障注入\n"
        "(进程杀死 + 心跳 reap_stale 联动) 属运行时验证, 已记后续。"
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
