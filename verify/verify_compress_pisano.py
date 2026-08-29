# -*- coding: utf-8 -*-
"""验证3: 皮萨诺记忆压缩引擎 — 无损往返 + 压缩率 (突破方案三).

验证《QunWork 7x24长程任务架构·创新研究与突破方案》5.2/5.4 的量化预期:
  * 周期模板 + 增量差分, 无损 roundtrip (decompress(compress(m)) == m);
  * 1000 条消息存储 2MB → 200KB (-90%);
  * 7×24 可支撑消息量 ~5000 → ~50000 条 (10x);
  * 解压延迟 <5ms。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from coworker.memory.compressor import PisanoMemoryCompressor, shortest_cycle  # noqa: E402

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


def make_messages(n: int, *, repeat: int = 3, noise: int = 0) -> list[dict]:
    """生成带周期性的真实消息序列 (user/assistant 交替 + 模板内容重复)。"""
    msgs = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        base = f"第 {i} 条消息: QunWork 7x24 长程任务架构研究文档第 {i // 10} 节讨论 " + ("黄金比例分形检查点引擎与蜂巢心跳模型的集成设计。" * repeat)
        content = base if noise == 0 else base + (" " * noise * (i % 5))
        msgs.append({"role": role, "content": content})
    return msgs


def main() -> int:
    print("=" * 72)
    print("  验证3: 皮萨诺记忆压缩 — 无损往返 × 压缩率 × 容量")
    print("=" * 72)

    comp = PisanoMemoryCompressor()

    # --- 3a. 周期检测 ---------------------------------------------------------
    print("\n[3a] 消息类型最短周期检测:")
    roles = ["user", "assistant"] * 10
    p = shortest_cycle(roles)
    print(f"  user/assistant 交替序列周期 = {p}")
    check("交替序列周期 = 2", p == 2)
    check("空序列周期 = 0", shortest_cycle([]) == 0)

    # --- 3b. 无损往返 ---------------------------------------------------------
    print("\n[3b] 无损 roundtrip (解压 == 原序列):")
    for n in (10, 100, 1000):
        msgs = make_messages(n)
        c = comp.compress_messages(msgs)
        back = comp.decompress(c)
        exact = back == msgs
        print(f"  {n} 条: period={c['period']}, roundtrip exact={exact}")
        check(f"{n} 条无损往返", exact)

    # --- 3c. 压缩率 (1000 条 → -90%) ------------------------------------------
    print("\n[3c] 压缩率 (方案预期: 1000 条 2MB → 200KB, -90%):")
    msgs = make_messages(1000, repeat=6)  # 高模板重复
    c = comp.compress_messages(msgs)
    raw_kb = c["raw_bytes"] / 1024
    packed_kb = c["packed_bytes"] / 1024
    ratio = c["compression_ratio"]
    print(f"  原始: {raw_kb:.0f} KB, 压缩后: {packed_kb:.0f} KB, 压缩率 {ratio:.1%}")
    check("压缩率 ≥ 90%", ratio >= 0.90, f"({ratio:.1%})")

    # --- 3d. 容量扩展 (7×24 可支撑消息量 10x) -----------------------------------
    print("\n[3d] 7×24 容量: 相同存储预算下可支撑消息量:")
    budget = 2 * 1024 * 1024  # 2MB 预算
    orig_capacity = 5000  # 当前 ~5000 条
    per_msg_compressed = c["packed_bytes"] / 1000
    new_capacity = int(budget / per_msg_compressed)
    print(f"  2MB 预算: 原支撑 ~{orig_capacity} 条 → 压缩后 ~{new_capacity} 条")
    check("容量提升 ≥ 10x", new_capacity >= 10 * orig_capacity, f"({new_capacity} 条)")

    # --- 3e. 解压延迟 <5ms -----------------------------------------------------
    print("\n[3e] 解压延迟:")
    t0 = time.perf_counter()
    comp.decompress(c)
    decomp_ms = (time.perf_counter() - t0) * 1000
    print(f"  1000 条解压: {decomp_ms:.2f} ms")
    check("解压延迟 <5ms", decomp_ms < 5.0, f"({decomp_ms:.2f}ms)")

    # --- 3f. 异构消息 (含 tool/system 与 dict content) --------------------------
    print("\n[3f] 异构消息 (4 种 role + 复杂 content):")
    mixed = []
    roles4 = ["user", "assistant", "tool", "system"]
    for i in range(48):
        mixed.append(
            {
                "role": roles4[i % 4],
                "content": [{"type": "text", "text": f"片段 {i} " + "模板内容" * 4}]
                if i % 3 == 0
                else f"纯文本 {i} 模板内容" * 2,
                "name": f"tool_{i % 3}" if i % 4 == 2 else None,
            }
        )
    c4 = comp.compress_messages(mixed)
    back4 = comp.decompress(c4)
    print(f"  48 条 4-role 消息: period={c4['period']}, ratio={c4['compression_ratio']:.1%}")
    check("4-role 消息无损往返", back4 == mixed)
    check("4-role 消息有压缩收益", c4["compression_ratio"] > 0.5)

    print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
