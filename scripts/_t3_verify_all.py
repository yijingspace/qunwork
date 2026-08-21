# -*- coding: utf-8 -*-
"""t3: W32 per-day distribution + diff shortstat + orchestration DB cross-check."""
import subprocess, sys, io, os, sqlite3, json
from collections import OrderedDict, Counter
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

def run(args):
    r = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', errors='replace')
    return r.stdout.strip()

print("========== [A] W32 window git per-day ==========")
out = run(['git','log','--since=2026-08-03','--until=2026-08-10',
           '--pretty=format:%ad|%h|%s','--date=format:%Y-%m-%d'])
lines = [l for l in out.splitlines() if l.strip()]
per_day = OrderedDict()
for l in lines:
    d, h, s = l.split('|', 2)
    per_day.setdefault(d, []).append((h, s))
total = sum(len(v) for v in per_day.values())
print("TOTAL:", total)
for d, v in per_day.items():
    print(f"  {d}: {len(v)}")
    for h, s in v:
        print(f"      {h} {s[:80]}")
with open('scripts/_t3_w32_log.txt','w',encoding='utf-8') as f:
    f.write(out)

print("\n========== [B] diff shortstat ed9297e..d149dc2 ==========")
print(run(['git','diff','--shortstat','ed9297e','d149dc2']))
n = len(run(['git','log','--oneline','ed9297e..d149dc2']).splitlines())
print("commits in range ed9297e..d149dc2:", n)

print("\n========== [C] W31 window git per-day ==========")
out31 = run(['git','log','--since=2026-07-27','--until=2026-08-03',
             '--pretty=format:%ad','--date=format:%Y-%m-%d'])
c31 = Counter(l for l in out31.splitlines() if l.strip())
print("TOTAL:", sum(c31.values()), dict(sorted(c31.items())))
print("W31 diff 56b3c62..ed9297e:", run(['git','diff','--shortstat','56b3c62','ed9297e']))

print("\n========== [D] real orchestration DB ==========")
dbpath = os.path.join(os.environ.get('APPDATA',''), 'Roaming', 'coworker', 'orchestration.db')
if not os.path.exists(dbpath):
    dbpath = os.path.join(os.environ.get('APPDATA',''), 'coworker', 'orchestration.db')
print("DB path:", dbpath, "exists:", os.path.exists(dbpath))
if os.path.exists(dbpath):
    con = sqlite3.connect(dbpath)
    cur = con.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    print("tables:", [r[0] for r in cur.fetchall()])
    # find the runs table schema
    for t in ['orchestration_runs','runs','orchestrate_runs']:
        try:
            cur.execute(f"PRAGMA table_info({t})")
            cols = [r[1] for r in cur.fetchall()]
            print(f"table {t} cols:", cols)
        except Exception as e:
            print(t, "err", e)
    con.close()
