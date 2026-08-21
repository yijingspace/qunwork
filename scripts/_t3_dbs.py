# -*- coding: utf-8 -*-
import sqlite3, sys, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

con = sqlite3.connect('.state/automation.db')

for t in ["scheduled_tasks", "task_runs"]:
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
    print(t, "cols:", cols)
    rows = con.execute(f"select * from {t} order by rowid desc limit 6").fetchall()
    for r in rows:
        print("  ", str(r)[:400])
    print()
