# -*- coding: utf-8 -*-
"""t4: inspect created_at format in real orchestration db."""
import sys, io, os, sqlite3
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

db = os.path.join(os.environ.get("APPDATA", ""), "coworker", "orchestration.db")
con = sqlite3.connect(db)
cur = con.cursor()
cur.execute("PRAGMA table_info(orchestration_runs)")
cols = [r[1] for r in cur.fetchall()]
print("columns:", cols)
cur.execute("SELECT created_at, status FROM orchestration_runs ORDER BY created_at")
rows = cur.fetchall()
for r in rows:
    print(repr(r[0])[:40], "|", r[1])
con.close()
