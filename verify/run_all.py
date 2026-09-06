# -*- coding: utf-8 -*-
"""QunWork 7×24 长程任务架构 — 全部验证脚本汇总运行器.

验证五大突破方案的**实际落地模块** (coworker.checkpoint / orchestrator.convergence /
memory.compressor / heartbeat / degradation), 非纯数学模拟。每个验证脚本独立
可运行 (exit code 0=全过, 1=有失败)。本脚本按顺序运行全部 5 个并汇总。
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

SCRIPTS = [
    "verify_checkpoint_fractal.py",  # 突破一: 分形检查点
    "verify_convergence_loopcoop.py",  # 突破二: LoopCoop 收敛
    "verify_compress_pisano.py",  # 突破三: 皮萨诺压缩
    "verify_heartbeat_honeycomb.py",  # 突破四: 蜂巢心跳
    "verify_degradation_fractal.py",  # 突破五: 分形降级
    # 握手打通（需要 OIR longrun 网关在 127.0.0.1:8787 运行）
    "verify_oir_longrun_handshake.py",  # QunWork 7×24 ↔ OIR longrun 握手契约
    "verify_oir_scheduler_driven.py",  # QunWork 真实 Scheduler 驱动 OIR longrun
]


def main() -> int:
    print("=" * 72)
    print("  QunWork 7×24 长程任务架构 — 全部落地验证")
    print("=" * 72)
    total_fail = 0
    for name in SCRIPTS:
        print("\n" + "=" * 72)
        print(f"  >>> {name}")
        print("=" * 72)
        rc = subprocess.call([PY, os.path.join(HERE, name)], env=os.environ.copy())
        total_fail += 1 if rc != 0 else 0
        print(f"  <<< {name}: {'PASS' if rc == 0 else 'FAIL'}")
    print("\n" + "=" * 72)
    print(f"  汇总: {len(SCRIPTS) - total_fail}/{len(SCRIPTS)} 个验证通过")
    print("=" * 72)
    return 1 if total_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
