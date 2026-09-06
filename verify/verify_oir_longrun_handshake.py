#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_oir_longrun_handshake.py — QunWork 7×24 ↔ OIR longrun 握手契约验证.

验证握手协议（docs/qunwork_longrun_handshake.md v1）可重复成立的六个断言：

  1. 网关握手: GET  /api/longrun/health  → horizon_ready=true
  2. 任务提交: POST /api/longrun/submit   → goal_id/oir_task_id 映射
  3. 分批推进: POST /api/longrun/advance  ×2 → completed_documents 递增
  4. 显式完成: POST /api/longrun/complete → OIR phase=Completed
  5. 状态回读: GET  /api/longrun/task     → 与推进一致
  6. 趋势回读: GET  /api/longrun/trend    → days 非空、growth_report 结构完整

前置: OIR longrun 网关已启动 (cargo run -p oir-core --example qunwork_longrun_bridge,
监听 127.0.0.1:8787)。独立可运行: exit 0 = 全过, 1 = 有失败。
仅标准库; 网络白名单与 oir_delegate 同纪律。
"""
from __future__ import annotations

import datetime
import ipaddress
import json
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

GATEWAY = "http://127.0.0.1:8787"
_ALLOWED_HOST_IP = ipaddress.ip_address("127.0.0.1")
_ALLOWED_PATHS = {
    "/api/longrun/health", "/api/longrun/submit", "/api/longrun/advance",
    "/api/longrun/complete", "/api/longrun/task", "/api/longrun/trend",
}

_checks: list[tuple[str, bool, str]] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    _checks.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))


def http_json(path: str, payload: dict | None = None, timeout: float = 30.0) -> dict:
    url = GATEWAY + path
    parsed = urllib.parse.urlparse(url)
    assert parsed.hostname and ipaddress.ip_address(parsed.hostname) == _ALLOWED_HOST_IP
    assert parsed.path in _ALLOWED_PATHS
    if payload is None:
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    print("=" * 66)
    print("  QunWork 7×24 ↔ OIR longrun 握手契约验证")
    print("=" * 66)

    goal_id = f"qw-verify-{datetime.datetime.now().strftime('%H%M%S')}"
    doc = {
        "name": "verify-probe.md",
        "text": ("QunWork 7x24 长程任务、OIR long_horizon 握手联测、概念索引、"
                 "跨日校准生长、蜂巢心跳、检查点、EDAMB、Universal Atom、谱隙收敛。"),
    }

    # 1) 握手
    try:
        health = http_json("/api/longrun/health")
        ok = bool(health.get("success") and health.get("data", {}).get("horizon_ready"))
        _check("网关握手 horizon_ready", ok, "health")
        if not ok:
            print("\n结论: FAIL — 网关未就绪（先启动 qunwork_longrun_bridge）")
            return 1
    except Exception as e:
        _check("网关握手 horizon_ready", False, str(e))
        return 1

    # 2) 提交
    try:
        r = http_json("/api/longrun/submit", {
            "goal_id": goal_id, "goal": "verify 契约", "total_documents": 2})
        data = r.get("data") or {}
        _check("任务提交 goal_id 映射", bool(r.get("success")) and data.get("goal_id") == goal_id
               and bool(data.get("oir_task_id")), f"oir_task_id={data.get('oir_task_id')}")
    except Exception as e:
        _check("任务提交 goal_id 映射", False, str(e)); return 1

    # 3) 分批推进 ×2（每批 1 篇）
    try:
        r1 = http_json("/api/longrun/advance", {
            "goal_id": goal_id, "documents": [doc], "max_terms": 20})
        d1 = r1.get("data") or {}
        r2 = http_json("/api/longrun/advance", {
            "goal_id": goal_id, "documents": [doc], "max_terms": 20})
        d2 = r2.get("data") or {}
        ok = r1.get("success") and r2.get("success") \
            and d2.get("completed_documents", 0) > d1.get("completed_documents", 0)
        _check("分批推进 completed 递增",
               ok, f"{d1.get('completed_documents')} -> {d2.get('completed_documents')}")
    except Exception as e:
        _check("分批推进 completed 递增", False, str(e)); return 1

    # 4) 显式完成 → 状态回读 phase
    try:
        http_json("/api/longrun/complete", {"goal_id": goal_id})
        st = http_json(f"/api/longrun/task?goal_id={goal_id}")
        d = st.get("data") or {}
        _check("complete 后 phase=Completed", st.get("success") and d.get("phase") == "Completed",
               f"phase={d.get('phase')}")
    except Exception as e:
        _check("complete 后 phase=Completed", False, str(e)); return 1

    # 5) 状态回读字段
    try:
        st = http_json(f"/api/longrun/task?goal_id={goal_id}")
        d = st.get("data") or {}
        need = {"goal_id", "oir_task_id", "phase", "completed_documents",
                "total_documents", "progress_percent"}
        ok = need.issubset(set(d.keys())) and d.get("completed_documents", 0) >= 2
        _check("状态回读字段齐全且累计正确", ok,
               f"completed={d.get('completed_documents')}/{d.get('total_documents')}")
    except Exception as e:
        _check("状态回读字段齐全且累计正确", False, str(e)); return 1

    # 6) 趋势回读
    try:
        tr = http_json("/api/longrun/trend")
        t = tr.get("data") or {}
        days = (t.get("trend") or {}).get("days") or {}
        gr = t.get("growth_report") or {}
        ok = tr.get("success") and len(days) >= 1 and bool(gr.get("direction"))
        _check("趋势回读 days + growth_report", ok,
               f"days={len(days)} direction={gr.get('direction')}")
    except Exception as e:
        _check("趋势回读 days + growth_report", False, str(e)); return 1

    print("=" * 66)
    n_fail = sum(1 for _, ok, _ in _checks if not ok)
    print(f"  汇总: {len(_checks) - n_fail}/{len(_checks)} 个断言通过")
    print("=" * 66)
    return 1 if n_fail else 0


if __name__ == "__main__":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
