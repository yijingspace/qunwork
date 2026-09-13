# -*- coding: utf-8 -*-
"""QunWork 7×24 长程任务架构 — 全部验证脚本汇总运行器.

验证五大突破方案的**实际落地模块** (coworker.checkpoint / orchestrator.convergence /
memory.compressor / heartbeat / degradation), 非纯数学模拟。每个验证脚本独立
可运行 (exit code 0=全过, 1=有失败)。本脚本按顺序运行全部脚本并汇总。

环境门控: 末两个 OIR 验证依赖仓库/CI 之外的东西(活着的网关、不在本仓库的 driver 模块),
缺条件时记 SKIP 并打印原因 —— 不算失败, 但也不会伪装成通过。此前它们在任何环境都 FAIL,
让这个门禁永远红着(GitHub CI run #1-#5 实证), 于是没人再看它。
"""
from __future__ import annotations

import importlib.util
import os
import socket
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

OIR_GATEWAY = ("127.0.0.1", 8787)


def _gateway_up() -> bool:
    """OIR longrun 网关是否在本机监听(握手脚本要连它读 /api/longrun/*)。"""
    try:
        with socket.socket() as s:
            s.settimeout(1.0)
            return s.connect_ex(OIR_GATEWAY) == 0
    except OSError:
        return False


def _has_driver() -> bool:
    """oir_longrun_driver 是 OIR 侧的产物, 不在本仓库 —— 没装就没法驱动真实 longrun。"""
    try:
        return importlib.util.find_spec("oir_longrun_driver") is not None
    except (ImportError, ValueError):
        return False


# script → (缺失条件说明, 检查函数)
GATED: dict[str, tuple[str, object]] = {
    "verify_oir_longrun_handshake.py": (f"需要运行中的 OIR longrun 网关 {OIR_GATEWAY[0]}:{OIR_GATEWAY[1]}", _gateway_up),
    "verify_oir_scheduler_driven.py": ("需要 oir_longrun_driver 模块(不在本仓库)", _has_driver),
}


def main() -> int:
    print("=" * 72)
    print("  QunWork 7×24 长程任务架构 — 全部落地验证")
    print("=" * 72)
    total_fail = 0
    passed = 0
    skipped = 0
    for name in SCRIPTS:
        gate = GATED.get(name)
        if gate is not None:
            reason, check = gate
            if not check():  # type: ignore[operator]
                print("\n" + "=" * 72)
                print(f"  >>> {name}")
                print(f"  [SKIP] {reason}")
                skipped += 1
                continue
        print("\n" + "=" * 72)
        print(f"  >>> {name}")
        print("=" * 72)
        # 子进程强制 UTF-8: Windows 控制台默认 GBK, 脚本打印 ₀/₂/⁻ 会 UnicodeEncodeError 而死
        # (CI 上由 workflow 级 PYTHONUTF8 兜住, 本地裸跑则不然)。
        env = os.environ.copy()
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        rc = subprocess.call([PY, os.path.join(HERE, name)], env=env)
        if rc == 0:
            passed += 1
        else:
            total_fail += 1
        print(f"  <<< {name}: {'PASS' if rc == 0 else 'FAIL'}")
    print("\n" + "=" * 72)
    summary = f"  汇总: {passed}/{len(SCRIPTS)} 个验证通过"
    if skipped:
        summary += f", {skipped} 个因缺环境跳过"
    if total_fail:
        summary += f", {total_fail} 个失败"
    print(summary)
    print("=" * 72)
    return 1 if total_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
