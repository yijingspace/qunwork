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
    python -m coworker.knowledge.purge --db ... --apply --backfill-hashes        # 先补 content_hash 再 retire
    python -m coworker.knowledge.purge --db ... --apply --purge-chunks           # 同时删 retire 条目的 chunks (不可逆, 空间回收)
    python -m coworker.knowledge.purge --db ... --apply --purge-chunks --vacuum  # 再 VACUUM (GB 级库分钟级)

规则 (路径驱动, dry-run 全部可见):
    R1 src-tauri 构建产物 bin/target 目录
    R2 Program Files 系统安装目录 (腾讯 Marvis 等)
    R3 .audit-* 测试临时残留
    R4 OIR 派生产物回灌 (title 以 概念索引_/术语表_ 开头且 kind=file)
    R5 盘根 workspace ('E:\\' 等) — 需显式 --rule-root, 默认关 (可能误杀)
    R6 同 fingerprint (mtime+size) 多路径副本 — 仅报告, 不自动 retire
    R7 跨 workspace 重复副本 (同一文件被嵌套扫描根各索引一份) — 自动退休余份,
       保留"有 chunks 且 workspace 最深"的那条 (内容不丢; 2026-09-13 owner-hit)
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
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


def purge_retired_chunks(
    c: sqlite3.Connection | sqlite3.Cursor, *, batch: int = 2000
) -> int:
    """Delete the chunks of every retired item, committing every ``batch`` rows.

    The live library is journal_mode=delete and ~6 GB, so a single monolithic DELETE builds
    a rollback journal the size of the deleted payload (~5 GB) that must fit on disk *before*
    it commits — on 2026-09-13 the volume reported 0 free bytes. Small transactions keep the
    journal at a few MB; the caller VACUUMs afterwards to hand the pages back to the OS.

    Accepts a connection or a cursor (``KnowledgeStore`` exposes the connection).
    """
    cur = c.cursor() if isinstance(c, sqlite3.Connection) else c
    con = cur.connection
    total = 0
    while True:
        cur.execute(
            "DELETE FROM knowledge_chunks WHERE rowid IN ("
            "  SELECT c.rowid FROM knowledge_chunks c"
            "  JOIN knowledge_items i ON i.id = c.item_id"
            "  WHERE i.retired = 1 LIMIT ?)",
            (batch,),
        )
        n = cur.rowcount
        con.commit()
        total += max(n, 0)
        if n < batch:
            return total


def _count_and_samples(c: sqlite3.Cursor, where: str, samples: int = 5) -> tuple[int, list]:
    rows = c.execute(
        f"SELECT id, workspace, title, substr(source_path,1,80) FROM knowledge_items "
        f"WHERE retired=0 AND {where} ORDER BY id LIMIT {samples + 1}",
    ).fetchall()
    total = c.execute(
        f"SELECT COUNT(*) FROM knowledge_items WHERE retired=0 AND {where}"
    ).fetchone()[0]
    return total, [dict(zip(("id", "workspace", "title", "path"), r)) for r in rows[:samples]]


def cross_workspace_duplicates(c: sqlite3.Cursor) -> tuple[list[dict], list[int]]:
    """Same file indexed as its own item in several workspaces (nested scan roots).

    Keyed by ``source_path``: an identical path IS the same file, so the extra rows are
    pure redundancy — indexing ``E:\\`` plus ``E:\\QunWork`` plus ``E:\\QunWork\\QunWork``
    gives every document one row per ancestor root (owner-hit 2026-09-13: 2,713 files,
    4,897 redundant rows, 96% of all chunks). ``content_hash`` is empty on legacy rows and
    ``fingerprint`` is mtime+size, which collides across *different* files, so neither can
    be the key here.

    Canonical row = has chunks, then the DEEPEST workspace that is a path prefix of the
    file (a specific project root beats a whole-disk root), then the lowest id. Everything
    else in the group is retired. A group whose canonical row has no chunks is left alone —
    retiring the others would destroy the only copy of the content.

    Returns ``(groups, retire_ids)``.
    """
    rows = c.execute(
        "SELECT id, workspace, source_path FROM knowledge_items "
        "WHERE retired=0 AND kind='file' AND source_path IS NOT NULL AND source_path<>'' "
        "ORDER BY id"
    ).fetchall()
    with_chunks = {
        r[0] for r in c.execute("SELECT DISTINCT item_id FROM knowledge_chunks")
    }

    groups: dict[str, list[tuple[int, str, str]]] = {}
    for item_id, workspace, source_path in rows:
        key = os.path.normcase(source_path)
        groups.setdefault(key, []).append((item_id, workspace or "", source_path))

    plan: list[dict] = []
    retire_ids: list[int] = []
    for key, members in groups.items():
        if len({m[1] for m in members}) < 2:
            continue  # single workspace → the per-path dedupe already handled it
        path = members[0][2]
        norm_path = os.path.normcase(path)

        def rank(m: tuple[int, str, str]) -> tuple[int, int, int]:
            item_id, workspace, _ = m
            prefix = 1 if workspace and norm_path.startswith(os.path.normcase(workspace)) else 0
            return (0 if item_id in with_chunks else 1, -prefix * len(workspace), item_id)

        ordered = sorted(members, key=rank)
        canonical = ordered[0]
        if canonical[0] not in with_chunks:
            continue  # no copy has content → refuse to retire anything
        losers = [m[0] for m in ordered[1:]]
        retire_ids.extend(losers)
        plan.append(
            {
                "path": path,
                "copies": len(members),
                "keep": {"id": canonical[0], "workspace": canonical[1]},
                "retire": [{"id": m[0], "workspace": m[1]} for m in ordered[1:]],
            }
        )
    return plan, retire_ids


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="知识库存量污染治理 (dry-run 默认)")
    ap.add_argument("--db", required=True)
    ap.add_argument("--apply", action="store_true", help="执行 retire (默认 dry-run)")
    ap.add_argument("--purge-chunks", action="store_true", help="同时 DELETE retire 条目的 chunks (不可逆)")
    ap.add_argument("--vacuum", action="store_true", help="最后 VACUUM 回收空间 (GB 级库分钟级)")
    ap.add_argument(
        "--chunk-batch",
        type=int,
        default=2000,
        help="每事务删除的 chunks 行数 (默认 2000: 大盘库避免 GB 级回滚日志)",
    )
    ap.add_argument("--undo-log", help="retire 回滚清单输出路径 (默认 <db>.retire-undo-<ts>.json)")
    ap.add_argument("--rule-root", action="store_true", help="启用 R5 盘根 workspace 规则 (默认关, 可能误杀)")
    ap.add_argument(
        "--backfill-hashes",
        nargs="?",
        const="active",
        choices=["active", "all"],
        help="先给存量条目补 content_hash (默认只补现役条目; all=连已退役条目一起补)",
    )
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    if not Path(args.db).is_file():
        print(f"❌ db 不存在: {args.db}")
        return 1

    # 存量补哈希 (R7 的跨路径去重与 purge 的内容规则都依赖它): 必须在主连接之前
    # 用 store 自己的连接完成, 避免两条连接同时写同一库。
    backfill_report: dict | None = None
    if args.backfill_hashes:
        if not args.apply:
            print("(dry-run) 加 --apply 才会补写 content_hash。")
        else:
            from .store import KnowledgeStore

            active_only = args.backfill_hashes == "active"
            store = KnowledgeStore(args.db)
            print(
                "补 content_hash 中 (优先重抽源文件, 源已丢失则用已存 chunks"
                f"{'; 仅现役条目' if active_only else ''})…",
                flush=True,
            )
            backfill_report = store.backfill_content_hashes(active_only=active_only)
            print(
                f"✅ 补哈希 {backfill_report['filled']} 条 "
                f"(重抽 {backfill_report['from_disk']} / chunks {backfill_report['from_chunks']} / "
                f"跳过 {backfill_report['skipped']}, 候选 {backfill_report['candidates']}"
                f", 退役未补 {backfill_report['skipped_retired']})"
            )
            store.close()

    con = sqlite3.connect(str(args.db))
    con.row_factory = sqlite3.Row
    c = con.cursor()

    rules = list(_PATH_RULES)
    if args.rule_root:
        rules.append(("R5", "盘根 workspace (E:\\ 等无界根)", "workspace IN ('E:\\','D:\\','C:\\')"))

    report: dict = {"db": args.db, "apply": args.apply, "rules": []}
    if backfill_report is not None:
        report["backfill_hashes"] = backfill_report
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

    # R7 跨 workspace 重复副本 — 同一文件在每个祖先扫描根各存一行 (可自动 retire:
    # canonical = 有 chunks 且 workspace 最深的那份, 内容不会丢)。
    plan, cross_ids = cross_workspace_duplicates(c)
    cross_rows = sum(len(g["retire"]) for g in plan)
    report["cross_workspace_groups"] = len(plan)
    report["cross_workspace_retire"] = cross_rows
    report["cross_workspace_samples"] = plan[:5]
    print(f"\n[R7] 跨 workspace 重复副本: {len(plan)} 个文件 / {cross_rows} 条冗余")
    for g in plan[:5]:
        print(f"    ×{g['copies']}  {g['path'][:90]}")
        print(f"        保留 #{g['keep']['id']} [{g['keep']['workspace'][:40]}]")
        for r in g["retire"][:3]:
            print(f"        退役 #{r['id']} [{r['workspace'][:40]}]")
        if len(g["retire"]) > 3:
            print(f"        … 其余 {len(g['retire']) - 3} 条")
    if cross_ids and args.apply:
        retire_ids.extend(cross_ids)

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
        # 回滚凭据: retire 是 UPDATE, 可原样撤回; chunks 删除不可逆, 但 R7 只退役有内容副本的
        # 冗余行, 内容在 canonical 条目里。留下 id 清单 = 一次 SQL 即可全部恢复。
        undo_path = Path(args.undo_log) if args.undo_log else Path(
            f"{args.db}.retire-undo-{int(time.time())}.json"
        )
        try:
            undo_path.write_text(
                json.dumps(
                    {
                        "created_at": int(time.time()),
                        "db": str(args.db),
                        "retired_ids": uniq,
                        "revert_sql": "UPDATE knowledge_items SET retired=0 WHERE id IN (...)",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            report["undo_log"] = str(undo_path)
            print(f"↩︎ 回滚清单: {undo_path}")
        except OSError as exc:  # 磁盘满/只读时不要吞掉已完成的 retire
            print(f"⚠️ 回滚清单写入失败 ({exc}); 已 retire 的 id 见 --json 输出的 retired 计数")
    if args.apply and args.purge_chunks:
        # 独立于本轮 retire: 对存量 retired 条目 (含此前批次的) 删 chunks 回收空间。
        print(f"删除 retired 条目 chunks 中 (每事务 {args.chunk_batch} 行)…", flush=True)
        deleted = purge_retired_chunks(c, batch=max(1, args.chunk_batch))
        report["chunks_deleted"] = deleted
        print(f"✅ 删除 retired 条目 chunks {deleted} 行 (不可逆; 条目行保留审计)")
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
