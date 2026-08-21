# -*- coding: utf-8 -*-
"""t3: orchestration DB run cross-check (W32 window runs + orch_121 final state + scheduled=0)."""
import sys, io, os, sqlite3, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

dbpath = os.path.join(os.environ.get('APPDATA',''), 'Roaming', 'coworker', 'orchestration.db')
if not os.path.exists(dbpath):
    dbpath = os.path.join(os.environ.get('APPDATA',''), 'coworker', 'orchestration.db')
print("DB:", dbpath, "exists:", os.path.exists(dbpath))
assert os.path.exists(dbpath), "DB not found"
con = sqlite3.connect(dbpath)
con.row_factory = sqlite3.Row
cur = con.cursor()

print("\n== ALL runs (id | status | created_at | updated_at | final | intent) ==")
cur.execute("SELECT run_id, status, created_at, updated_at, final, intent FROM orchestration_runs ORDER BY created_at")
rows = cur.fetchall()
print("total runs:", len(rows))
for r in rows:
    fin = 'T' if r['final'] else 'F'
    print(f"{r['run_id']} | {r['status']:<12} | {r['created_at']} | {r['updated_at']} | fin={fin} | {str(r['intent'])[:60]}")

print("\n== W32 window runs (created 2026-08-05 .. 2026-08-09) ==")
cur.execute("SELECT run_id, status, created_at, updated_at, final, intent FROM orchestration_runs WHERE created_at >= '2026-08-05' AND created_at < '2026-08-10' ORDER BY created_at")
rows = cur.fetchall()
print("window runs:", len(rows))
from collections import Counter
st = Counter(r['status'] for r in rows)
print("status tally:", dict(st))
for r in rows:
    print(f"  {r['run_id']} | {r['status']:<12} | {r['created_at']} | {r['updated_at']} | fin={r['final']}")

print("\n== orch_121 detail ==")
cur.execute("SELECT * FROM orchestration_runs WHERE run_id LIKE '%121%'")
for r in cur.fetchall():
    print(dict(r))

print("\n== orch_121 events (last 25) ==")
try:
    cur.execute("SELECT * FROM orchestration_events WHERE run_id LIKE '%121%' ORDER BY rowid DESC LIMIT 25")
    evs = cur.fetchall()
    print("events:", len(evs))
    for e in evs:
        print(" ", dict(e))
except Exception as ex:
    print("err", ex)

print("\n== scheduled / automation runs check ==")
cur.execute("SELECT run_id, status, intent FROM orchestration_runs WHERE intent LIKE '%scheduled%' OR intent LIKE '%定时%' OR intent LIKE '%Scheduled%'")
print("runs with scheduled-like intent:", cur.fetchall())

con.close()
