#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""knowledge purge — 知识库存量污染治理 CLI (2026-09-06)。

背景 (主会话实证): items 2.2k → 21.9k 爆炸, 8-30 一天 +19384 条 —
旧扫描把 tauri 构建产物 / 腾讯 Marvis 安装目录 / .audit-* 测试残留
吞进库, 且「已索引父目录自我扩散」放大。入库侧防御见 store.py
(_SKIP_DIRS 扩充 / _scan_targets 收敛 / content_hash 去重);
本工具治理存量: 按规则 retire (retired=1, 审计保留, 不删行)。

用法:
    python -m coworker.knowledge.purge --db E:\\QunWork\\knowledge.db           # dry-run 预览
    python -m coworker.knowledge.purge --db ... --apply                          # 执行 retire
    python -m coworker.knowledge.purge --db ... --apply --purge-chunks           # 同时删 retire 条目的 chunks (不可逆, 空间回收)
    python -m coworker.knowledge.purge --db ... --apply --purge-chunks --vacuum  # 再 VACUUM (GB 级库分钟级)

规则 (v1, 路径驱动, dry-run 全部可见):
    R1 src-tauri 构建产物 bin/target 目录
    R2 Program Files 系统安装目录 (腾讯 Marvis 等)
    R3 .audit-* 测试临时残留
    R4 OIR 派生产物回灌 (title 以 概念索引_/术语表_ 开头且 kind=file)
    R5 盘根 workspace ('E:\\' 等) — 需显式 --rule-root, 默认关 (可能误杀)
    R6 同 fingerprint (mtime+size) 多路径副本 — 仅报告, 不自动 retire
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# (rule_id, 说明, WHERE 片段/特殊处理)
_PATH_RULES = [
    ("R1", "src-tauri 构建面 (bin/binaries/target)",
     "source_path LIKE '%\\src-tauri\\%'"),
    ("R2", "Program Files 系统安装目录",
     "source_path LIKE '%\\Program Files\\%' OR source_path LIKE '%\\Program Files (x86)\\%'"),
    ("R3", ".audit-* 测试临时残留",
     "source_path LIKE '%\\.audit-%'"),
    ("R4", "OIR 派生产物回灌 (概念索引_/术语表_)",
     "kind='file' AND (title LIKE '概念索引\\_%' ESCAPE '\\' OR title LIKE '术语表\\_%' ESCAPE '\\')"),
]


def _count_and_samples(c: sqlite3.Cursor, where: str, samples: int = 5) -> tuple[int, list]:
    rows = c.execute(
        f"SELECT id, workspace, title, substr(source_path,1,80) FROM knowledge_items "
        f"WHERE retired=0 AND {where} ORDER BY id LIMIT {samples + 1}",
    ).fetchall()
    total = c.execute(
        f"SELECT COUNT(*) FROM knowledge_items WHERE retired=0 AND {where}"
    ).fetchone()[0]
    return total, [dict(zip(("id", "workspace", "title", "path"), r)) for r in rows[:samples]]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="知识库存量污染治理 (dry-run 默认)")
    ap.add_argument("--db", required=True)
    ap.add_argument("--apply", action="store_true", help="执行 retire (默认 dry-run)")
    ap.add_argument("--purge-chunks", action="store_true", help="同时 DELETE retire 条目的 chunks (不可逆)")
    ap.add_argument("--vacuum", action="store_true", help="最后 VACUUM 回收空间 (GB 级库分钟级)")
    ap.add_argument("--rule-root", action="store_true", help="启用 R5 盘根 workspace 规则 (默认关, 可能误杀)")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    if not Path(args.db).is_file():
        print(f"❌ db 不存在: {args.db}")
        return 1
    con = sqlite3.connect(str(args.db))
    con.row_factory = sqlite3.Row
    c = con.cursor()

    rules = list(_PATH_RULES)
    if args.rule_root:
        rules.append(("R5", "盘根 workspace (E:\\ 等无界根)", "workspace IN ('E:\\','D:\\','C:\\')"))

    report: dict = {"db": args.db, "apply": args.apply, "rules": []}
    retire_ids: list[int] = []
    print("═" * 60)
    print(f"知识库污染治理 — {'APPLY' if args.apply else 'DRY-RUN (预览)'}")
    print(f"  db: {args.db}")
    print("═" * 60)

    for rid, desc, where in rules:
        total, samples = _count_and_samples(c, where)
        entry = {"rule": rid, "desc": desc, "matches": total, "samples": samples}
        report["rules"].append(entry)
        print(f"\n[{rid}] {desc}: {total} 条")
        for s in samples:
            print(f"    #{s['id']} [{s['workspace'][:30]}] {s['title'][:40]} ← {s['path']}")
        if total > len(samples):
            print(f"    … 以及另外 {total - len(samples)} 条")
        if total and args.apply:
            ids = [r[0] for r in c.execute(
                f"SELECT id FROM knowledge_items WHERE retired=0 AND {where}")]
            retire_ids.extend(ids)

    # R6 副本指纹组 — 仅报告
    dup_groups = c.execute(
        "SELECT fingerprint, COUNT(*) n, COUNT(DISTINCT source_path) dp FROM knowledge_items "
        "WHERE retired=0 AND fingerprint IS NOT NULL AND source_path IS NOT NULL "
        "GROUP BY fingerprint HAVING n>1 AND dp>1 ORDER BY n DESC LIMIT 10"
    ).fetchall()
    dup_items = sum(g[1] - 1 for g in dup_groups)
    report["duplicate_fingerprint_groups"] = len(dup_groups)
    report["duplicate_report_note"] = "同 fingerprint 多路径副本 — 仅报告, 人工决定 (canonical 保留策略未定)"
    print(f"\n[R6] 同 fingerprint 多路径副本: {len(dup_groups)} 组 / 约 {dup_items} 条冗余 (仅报告)")
    for g in dup_groups[:5]:
        paths = [r[0][:60] for r in c.execute(
            "SELECT source_path FROM knowledge_items WHERE fingerprint=? AND retired=0 LIMIT 4",
            (g[0],))]
        for pp in paths:
            print(f"    {pp}")
        if len(paths) < min(g[1], 4):
            print("    …")

    before = c.execute("SELECT COUNT(*) FROM knowledge_items WHERE retired=0").fetchone()[0]
    report["active_before"] = before

    if args.apply and retire_ids:
        uniq = sorted(set(retire_ids))
        c.executemany(
            "UPDATE knowledge_items SET retired=1 WHERE id=?", [(i,) for i in uniq]
        )
        con.commit()
        report["retired"] = len(uniq)
        print(f"\n✅ retire {len(uniq)} 条 (审计保留, search 不再返回)")
    if args.apply and args.purge_chunks:
        # 独立于本轮 retire: 对存量 retired 条目 (含此前批次的) 删 chunks 回收空间。
        cur = c.execute(
            "DELETE FROM knowledge_chunks WHERE item_id IN (SELECT id FROM knowledge_items WHERE retired=1)"
        )
        con.commit()
        report["chunks_deleted"] = cur.rowcount
        print(f"✅ 删除 retired 条目 chunks {cur.rowcount} 行 (不可逆; 条目行保留审计)")
        if args.vacuum:
            print("VACUUM 中 (GB 级库需数分钟)…", flush=True)
            c.execute("VACUUM")
            print("✅ VACUUM 完成")
    elif not args.apply:
        print(f"\n(dry-run) 加 --apply 执行 retire; 现役 {before} 条")

    after = c.execute("SELECT COUNT(*) FROM knowledge_items WHERE retired=0").fetchone()[0]
    report["active_after"] = after
    print(f"\n现役条目: {before} → {after}")
    con.close()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
