# -*- coding: utf-8 -*-
"""t5: query REAL coworker data dir DBs (knowledge.db / orchestration.db / automation.db / coworker.db)."""
import sqlite3, io, sys, os

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

base = os.path.join(os.environ["APPDATA"], "coworker")

def dump(dbpath, max_tabs=20):
    print("=" * 20, dbpath)
    if not os.path.exists(dbpath):
        print("MISSING")
        return
    con = sqlite3.connect(dbpath)
    tabs = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    for t in tabs[:max_tabs]:
        try:
            n = con.execute("select count(*) from " + t).fetchone()[0]
            print(f"  {t} = {n}")
        except Exception as e:
            print("  ", t, "ERR", e)
    con.close()

for db in ["knowledge.db", "orchestration.db", "automation.db", "coworker.db"]:
    dump(os.path.join(base, db))

# knowledge item kinds + recent items
print("=" * 20, "knowledge kinds")
con = sqlite3.connect(os.path.join(base, "knowledge.db"))
try:
    rows = con.execute("select kind, count(*) from knowledge_items group by kind order by 2 desc").fetchall()
    for r in rows:
        print(" ", r)
    print("  total knowledge_items:", con.execute("select count(*) from knowledge_items").fetchone()[0])
    print("  total knowledge_chunks:", con.execute("select count(*) from knowledge_chunks").fetchone()[0])
    # sample created_at range
    row = con.execute("select min(created_at), max(created_at) from knowledge_items").fetchone()
    print("  created_at range:", row)
    row = con.execute("select count(distinct workspace) from knowledge_items").fetchone()
    print("  distinct workspaces:", row)
except Exception as e:
    print("ERR", e)
con.close()

# orchestration runs status
print("=" * 20, "orchestration runs")
con = sqlite3.connect(os.path.join(base, "orchestration.db"))
try:
    tabs = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    print("tables:", tabs)
    cols = [c[1] for c in con.execute("pragma table_info(orchestration_runs)")]
    print("cols:", cols)
    for c in cols:
        if c in ("status", "intent"):
            try:
                rows = con.execute(f"select {c}, count(*) from orchestration_runs group by {c} order by 2 desc").fetchall()
                print(f"by {c}:", rows)
            except Exception as e:
                print("ERR", e)
    row = con.execute("select min(created_at), max(created_at) from orchestration_runs").fetchone()
    print("created_at range:", row)
    total = con.execute("select count(*) from orchestration_runs").fetchone()[0]
    print("total runs:", total)
except Exception as e:
    print("ERR", e)
con.close()

# automation tasks
print("=" * 20, "automation")
con = sqlite3.connect(os.path.join(base, "automation.db"))
try:
    tabs = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    print("tables:", tabs)
    for t in tabs:
        n = con.execute(f"select count(*) from {t}").fetchone()[0]
        print(f"  {t} = {n}")
        if n and t == "scheduled_tasks":
            rows = con.execute(f"select * from {t} limit 5").fetchall()
            for r in rows:
                print("   ", r)
except Exception as e:
    print("ERR", e)
con.close()
