# -*- coding: utf-8 -*-
"""查询 08-03..08-09 窗口内蜂群编排 run 状态分布。"""
import sqlite3, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
db = r"C:\Users\Administrator\AppData\Roaming\coworker\orchestration.db"
con = sqlite3.connect(db)
cur = con.cursor()
cur.execute("SELECT run_id, status, created_at, intent FROM runs WHERE created_at >= '2026-08-03' AND created_at < '2026-08-10' ORDER BY created_at")
rows = cur.fetchall()
print("runs in 08-03..08-09:", len(rows))
from collections import Counter
print("status dist:", dict(Counter(r[1] for r in rows)))
for r in rows:
    print("  %s | %s | %s | %s" % (r[0], r[1], r[2], r[3][:60]))
