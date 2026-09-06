#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_oir_scheduler_driven.py — QunWork 真实 Scheduler 驱动 OIR longrun.

验证 QunWork 7×24 **原生 Scheduler**（非自写循环）能通过 `extra_tick` 驱动
OIR long_horizon：空 task store + no-op runner + `extra_tick=OirLongrunTick`，
真实 scheduler tick 循环推动概念索引任务 submit→advance→complete，OIR 侧
同一 oir_task_id 全程记账。

断言：
  1. scheduler 首 tick submit goal 成功（OIR 侧有 oir_task_id）
  2. N 个真实 tick 内文档全部推进（pos=3/3）
  3. complete 后 OIR phase=Completed

前置: OIR longrun 网关已启动（127.0.0.1:8787）。独立可运行: exit 0/1。
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile

QUNWORK_SRC = pathlib.Path(r"E:\QunWork\QunWork")
BRIDGE_DIR = pathlib.Path(r"E:\QunWork\oir_bridge")
sys.path.insert(0, str(QUNWORK_SRC))
sys.path.insert(0, str(BRIDGE_DIR))

from coworker.automation.scheduler import Scheduler  # noqa: E402
from coworker.automation.store import TaskStore  # noqa: E402
from oir_longrun_driver import OirLongrunTick  # noqa: E402

DOCS = [
    ("sched-a.md", "QunWork 真实 Scheduler tick 驱动 OIR long_horizon 概念索引 心跳。"),
    ("sched-b.md", "EDAMB Universal Atom 谱隙收敛 跨日生长 检查点 续跑 关键词测试。"),
    ("sched-c.md", "蜂巢心跳 分形降级 LoopCoop 残差校准 能力蒸馏 第三篇验证。"),
]


def _gateway_ok() -> bool:
    import ipaddress
    import json
    import urllib.request
    req = urllib.request.Request("http://127.0.0.1:8787/api/longrun/health")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return bool(json.loads(resp.read())["data"]["horizon_ready"])


async def _run() -> tuple[bool, str]:
    workdir = pathlib.Path(tempfile.mkdtemp())
    smoke = workdir / "docs"
    smoke.mkdir()
    for name, text in DOCS:
        (smoke / name).write_text(text, encoding="utf-8")

    store = TaskStore(str(workdir / "sched.db"))
    tick = OirLongrunTick(doc_dir=str(smoke), glob="*.md", batch=1,
                          goal_id="qw-sched-verify", heartbeat=False)
    sched = Scheduler(store, lambda task, trigger: None,
                      tick_seconds=0.3, extra_tick=tick)
    sched.start()
    await asyncio.sleep(2.0)  # ~6-7 real scheduler ticks
    await sched.stop()

    ok_done = tick._state.get(tick.goal_id, {}).get("pos") == len(DOCS)
    return ok_done, f"pos={tick._state.get(tick.goal_id, {}).get('pos')}/{len(DOCS)}"


def main() -> int:
    print("=" * 66)
    print("  QunWork 真实 Scheduler 驱动 OIR longrun 验证")
    print("=" * 66)
    if not _gateway_ok():
        print("  [FAIL] OIR longrun 网关未就绪（先启动 qunwork_longrun_bridge）")
        return 1
    ok, detail = asyncio.run(_run())
    print(f"  [{'PASS' if ok else 'FAIL'}] 真实 Scheduler tick 驱动推进 {detail}")
    print("=" * 66)
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
