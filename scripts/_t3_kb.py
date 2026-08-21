# -*- coding: utf-8 -*-
"""T3: inspect memory + knowledge DBs for weekly-report related entries."""
import sqlite3, sys, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

for db in [".qunwork/memory.db", ".coworker/knowledge.db"]:
    print("==== %s" % db)
    try:
        con = sqlite3.connect(db)
        tabs = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
        print("tables:", tabs)
        for t in tabs:
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
            print(" ", t, cols)
            try:
                n = con.execute(f"select count(*) from {t}").fetchone()[0]
                print("   rows:", n)
                if n and n <= 30:
                    for row in con.execute(f"select * from {t} limit 8"):
                        s = str(row)
                        if any(k in s for k in ["周报", "weekly", "roadmap", "计划", "目标", "P0"]):
                            print("   *", s[:300])
            except Exception as e:
                print("   err", e)
    except Exception as e:
        print("ERR", e)
    print()
