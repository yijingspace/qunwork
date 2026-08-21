# -*- coding: utf-8 -*-
"""查询 W32 窗口蜂群 run（先探测表名）。"""
import sqlite3, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
db = r"C:\Users\Administrator\AppData\Roaming\coworker\orchestration.db"
con = sqlite3.connect(db)
cur = con.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
print("tables:", [r[0] for r in cur.fetchall()])
# try common table names
for t in ["orchestration_runs", "runs", "orchestrate_runs", "swarm_runs"]:
    try:
        cur.execute("SELECT * FROM %s LIMIT 1" % t)
        print("table %s OK, cols:" % t, [d[0] for d in cur.description])
        break
    except sqlite3.OperationalError as e:
        print("table %s: %s" % (t, e))
