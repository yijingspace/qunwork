# -*- coding: utf-8 -*-
"""Compact per-run summary (id, dates, status, dur, intent) + automation check."""
import sqlite3, datetime
from pathlib import Path

db = str(Path.home() / "AppData/Roaming/coworker/orchestration.db")
con = sqlite3.connect(db)
print("RUN\tCREATED\tUPDATED\tSTATUS\tDUR\tINTENT")
for r in con.execute("SELECT run_id,intent,status,created_at,updated_at FROM orchestration_runs ORDER BY created_at"):
    rid, intent, status, c, u = r
    ciso = datetime.datetime.fromtimestamp(c).strftime("%m-%d %H:%M")
    uiso = datetime.datetime.fromtimestamp(u).strftime("%m-%d %H:%M") if u else "-"
    dur = round(u - c, 1) if u else -1
    print(f"{rid[:8]}\t{ciso}\t{uiso}\t{status}\t{dur}\t{intent[:46]}")
print()
print("EVENT-KINDS:", [k[0] for k in con.execute(
    "SELECT DISTINCT kind FROM orchestration_events ORDER BY kind")])
print("TOTAL RUNS:", con.execute("SELECT COUNT(*) FROM orchestration_runs").fetchone()[0])
con.close()

print()
print("== automation.db (real) ==")
con = sqlite3.connect(str(Path.home() / "AppData/Roaming/coworker/automation.db"))
for t in ["scheduled_tasks", "task_runs"]:
    try:
        cols = [c[1] for c in con.execute(f"PRAGMA table_info({t})")]
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"-- {t}: rows={n} cols={cols}")
        for r in con.execute(f"SELECT * FROM {t} LIMIT 30"):
            print("   ", tuple(str(x)[:70] for x in r))
    except Exception as e:
        print("--", t, "ERR", e)
con.close()
