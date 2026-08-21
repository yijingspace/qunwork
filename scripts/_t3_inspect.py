# -*- coding: utf-8 -*-
"""T3: dump schema + row counts of every .state / knowledge DB."""
import sqlite3, sys, os, glob
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

dbs = [".state/automation.db", ".state/coworker.db", ".state/orchestration.db",
       ".coworker/knowledge.db", ".qunwork/memory.db"]
for db in dbs:
    print("=" * 70)
    print("DB:", db, "size:", os.path.getsize(db) if os.path.exists(db) else "MISSING")
    if not os.path.exists(db):
        continue
    con = sqlite3.connect(db)
    tabs = [r[0] for r in con.execute(
        "select name from sqlite_master where type='table' and name not like 'sqlite_%'")]
    for t in tabs:
        try:
            n = con.execute(f"select count(*) from '{t}'").fetchone()[0]
            cols = [r[1] for r in con.execute(f"PRAGMA table_info('{t}')")]
            print(f"  {t} ({n} rows): {cols}")
        except Exception as e:
            print(f"  {t}: ERR {e}")
    con.close()
