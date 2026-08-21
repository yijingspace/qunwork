# -*- coding: utf-8 -*-
"""T3: 组织资产使用次数分析 (asset_usage).

数据源:
  A. .state 各 DB (automation/coworker/orchestration) + .coworker/knowledge.db + .qunwork/memory.db
  B. catalog.py 能力目录 (coworker/catalog.py) 及全库对其 capability id 的引用
  C. inventory.py / 资产文档语料 (md/txt/json,排除索引跳过目录)
  D. _swarm_reports/ 自动化产物 + 根目录周报产物 + .qunwork/weekly 数据

对每个资产统计:
  ref_occur  = 其他文档中的引用出现次数 (按别名子串匹配)
  ref_docs   = 引用该资产的去重文档数
  git_touches= git log 中触及该资产文件的提交数
  db_use     = knowledge_items.use_count (DB 实际记录;本实例为空 → 0)
  热度       = hot / warm / cold 分级
"""
import sqlite3, sys, os, re, glob, json, subprocess
from collections import defaultdict, Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.getcwd()
SKIP_DIRS = {".git", ".svn", "node_modules", ".venv", "venv", ".coworker", ".qunwork",
             ".state", ".tmp-state", "_swarm_reports", "target", "dist", "build",
             "site-packages", ".reasonix", "backups", "egg-info", "__pycache__",
             ".github", "packaging", "docs", ".coworker"}
DOC_EXTS = {".md", ".markdown", ".txt", ".rst", ".csv", ".log", ".json"}


def walk_docs(root=ROOT):
    """所有可作为知识/自动化产物的文档文件(排除跳过目录)。"""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        for fn in filenames:
            if fn.startswith("_t3_") or fn.startswith("_t1_") or fn.startswith("_t2_") or fn.startswith("_t4_"):
                continue
            if os.path.splitext(fn)[1].lower() in DOC_EXTS:
                p = os.path.join(dirpath, fn)
                rel = os.path.relpath(p, root)
                out.append(rel)
    return sorted(out)


def read(rel):
    try:
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


# ---------------- A. 各 DB 现状 ----------------
print("=" * 72)
print("A. .state 各 DB / 知识库 / 记忆库 现状")
print("=" * 72)
db_report = {}
for db in [".state/automation.db", ".state/coworker.db", ".state/orchestration.db",
           ".coworker/knowledge.db", ".qunwork/memory.db"]:
    con = sqlite3.connect(db)
    info = {}
    for t, in con.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'"):
        info[t] = con.execute(f"select count(*) from '{t}'").fetchone()[0]
    db_report[db] = info
    print(f"  {db}: {info}")
    con.close()

# knowledge_items 明细(空库也列出字段基线)
con = sqlite3.connect(".coworker/knowledge.db")
cols = [r[1] for r in con.execute("PRAGMA table_info(knowledge_items)")]
print("  knowledge_items 字段:", cols)
con.close()

# ---------------- B/C/D. 资产清单 ----------------
print()
print("=" * 72)
print("B/C/D. 资产清单构建")
print("=" * 72)
docs = walk_docs()
print(f"  语料文档数: {len(docs)}")

# 资产分类
knowledge_docs = [d for d in docs if not d.startswith("_swarm_reports") and not d.startswith("周报") and "weekly" not in d]
automation_artifacts = [d for d in docs if d.startswith("_swarm_reports") or d.startswith("周报") or ".qunwork/weekly" in d]

print(f"  知识库类资产(文档): {len(knowledge_docs)}")
for d in knowledge_docs:
    print("    K", d)
print(f"  自动化产物类资产: {len(automation_artifacts)}")
for d in automation_artifacts:
    print("    A", d)

# 记忆(数据库内)
con = sqlite3.connect(".qunwork/memory.db")
mem_rows = con.execute("select id, phase, created_at from vector_memories").fetchall()
con.close()
print(f"  记忆库条目: {len(mem_rows)} 条 (phase 分布: {Counter(r[1] for r in mem_rows)})")

# 目录能力(来自 catalog.py 的静态清单)
cap_ids = ["code_files", "files", "git", "search", "shell", "todo"]
print(f"  目录能力(catalog.py): {cap_ids}")

# ---------------- 别名表 ----------------
def stem_of(rel):
    base = os.path.basename(rel)
    return os.path.splitext(base)[0]

def aliases_of(rel):
    """为资产生成匹配别名:完整 stem、去时间戳 stem、中文显著片段。"""
    stem = stem_of(rel)
    al = [stem]
    # 去前缀时间戳 20260805-235904- / 20260806-001209-
    m = re.match(r"^\d{8}-\d{6}-(.+)$", stem)
    if m:
        al.append(m.group(1))
    m2 = re.match(r"^(data_\d{4}W\d+)", stem)
    if m2:
        al.append(m2.group(1))
    # 中文标题段(取连续中文>=4字)
    for seg in re.findall(r"[\u4e00-\u9fff]{4,}", stem):
        al.append(seg)
    # 根目录周报: 周报_2026W32_目标达成度评估 → 目标达成度评估 等
    return sorted(set(a for a in al if len(a) >= 3))

assets = {}
for rel in knowledge_docs + automation_artifacts:
    assets[rel] = {
        "kind": "automation_artifact" if rel in automation_artifacts else "knowledge_doc",
        "aliases": aliases_of(rel),
    }

# 补充非文档资产
assets["catalog:code_files"] = {"kind": "catalog_capability", "aliases": ["code_files"]}
assets["catalog:files"] = {"kind": "catalog_capability", "aliases": ["files"]}
assets["catalog:git"] = {"kind": "catalog_capability", "aliases": ["git"]}
assets["catalog:search"] = {"kind": "catalog_capability", "aliases": ["search"]}
assets["catalog:shell"] = {"kind": "catalog_capability", "aliases": ["shell"]}
assets["catalog:todo"] = {"kind": "catalog_capability", "aliases": ["todo"]}
assets["memory.db:6条"] = {"kind": "memory_entry", "aliases": []}
assets["knowledge.db:use_count"] = {"kind": "db_signal", "aliases": []}

# ---------------- 引用计数 ----------------
corpus = {}
for rel in docs:
    corpus[rel] = read(rel)

usage = {}
for asset, meta in assets.items():
    if meta["kind"] in ("memory_entry", "db_signal"):
        usage[asset] = {"kind": meta["kind"], "ref_occur": 0, "ref_docs": 0, "git_touches": 0}
        continue
    occ = 0
    refdocs = set()
    for rel, text in corpus.items():
        if rel == asset:
            continue
        n = 0
        for al in meta["aliases"]:
            n += text.count(al)
        if n:
            occ += n
            refdocs.add(rel)
    # git 触及次数
    gt = 0
    if asset.startswith("catalog:"):
        gt = 0  # 代码内引用单独统计
    else:
        try:
            out = subprocess.run(["git", "log", "--oneline", "--", asset],
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=30)
            gt = len([l for l in out.stdout.splitlines() if l.strip()])
        except Exception:
            gt = 0
    usage[asset] = {"kind": meta["kind"], "ref_occur": occ, "ref_docs": len(refdocs),
                    "git_touches": gt}

# catalog 能力引用:统计 catalog.py 之外的引用点(代码+文档+persona)
cap_refs = {}
for cap in cap_ids:
    n = 0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            if p.endswith((".py", ".md", ".toml", ".json")):
                if "catalog.py" in p or fn.startswith(("_t1_", "_t3_", "_t4_", "_t2_")):
                    continue
                try:
                    t = open(p, encoding="utf-8", errors="replace").read()
                except Exception:
                    continue
                # 词边界匹配能力 id(避免 files/search 等通用词误计,要求出现在工具列表/引用语境)
                n += len(re.findall(rf"\b{re.escape(cap)}\b", t))
    cap_refs[cap] = n
    usage[f"catalog:{cap}"]["ref_occur"] = cap_refs[cap]

# ---------------- 输出统计表 ----------------
print()
print("=" * 72)
print("asset_usage 统计表")
print("=" * 72)
header = f"{'资产':<58}{'类型':<18}{'引用次数':>6}{'引用文档':>6}{'git触及':>6}{'db_use':>7}  {'热度'}"
print(header)
print("-" * len(header.encode("gbk", errors="replace")) if False else "-" * 110)

def classify(u):
    total = u["ref_occur"] + u["git_touches"]
    if total >= 5 or u["ref_occur"] >= 3:
        return "HOT"
    if total >= 1:
        return "warm"
    return "cold"

rows = []
for asset, u in sorted(usage.items(), key=lambda kv: -(kv[1]["ref_occur"] + kv[1]["git_touches"])):
    if u["kind"] in ("memory_entry", "db_signal"):
        continue
    hot = classify(u)
    rows.append((asset, u, hot))
    disp = asset if len(asset) <= 56 else asset[:53] + "..."
    print(f"{disp:<58}{u['kind']:<18}{u['ref_occur']:>6}{u['ref_docs']:>6}{u['git_touches']:>6}{'0':>7}  {hot}")

hot = [r for r in rows if r[2] == "HOT"]
warm = [r for r in rows if r[2] == "warm"]
cold = [r for r in rows if r[2] == "cold"]
print()
print(f"汇总: 资产总数 {len(rows)} | HOT {len(hot)} | warm {len(warm)} | cold {len(cold)}")

# ---------------- 保存 JSON ----------------
out = {
    "generated_at": "2026-08-06",
    "task": "t3 组织资产使用次数分析",
    "db_snapshot": db_report,
    "knowledge_items_columns": cols,
    "memory_phases": dict(Counter(r[1] for r in mem_rows)),
    "catalog_capability_refs": cap_refs,
    "assets": [
        {
            "asset": a, "kind": u["kind"], "ref_occur": u["ref_occur"],
            "ref_docs": u["ref_docs"], "git_touches": u["git_touches"],
            "db_use_count": 0, "heat": classify(u),
        }
        for a, u, _ in rows
    ],
}
os.makedirs(".qunwork/asset_usage", exist_ok=True)
with open(".qunwork/asset_usage/asset_usage.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print("\nsaved .qunwork/asset_usage/asset_usage.json")
