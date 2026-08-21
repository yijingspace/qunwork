# -*- coding: utf-8 -*-
"""T2: probe knowledge/memory/orchestration DBs for last-7-days assets."""
import sqlite3, os, sys, io, datetime
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

now = datetime.datetime.now()
cutoff = now - datetime.timedelta(days=7)
print(f"now={now.isoformat()}  cutoff(7d)={cutoff.isoformat()}")

DBS = ['.coworker/knowledge.db', '.qunwork/memory.db',
       '.state/automation.db', '.state/orchestration.db', '.state/coworker.db']

def dump(db):
    print(f"\n===== {db} =====")
    if not os.path.exists(db):
        print("  (missing)"); return
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    tabs = [r[0] for r in con.execute("select name from sqlite_master where type='table' order by name")]
    for t in tabs:
        try:
            n = con.execute(f'select count(*) from "{t}"').fetchone()[0]
        except Exception as e:
            print(f"  {t}: ERR {e}"); continue
        print(f"  -- {t} (rows={n})")
        if n == 0 or n > 500:
            continue
        cols = [c[1] for c in con.execute(f'PRAGMA table_info("{t}")')]
        for row in con.execute(f'select * from "{t}"'):
            d = dict(row)
            # find date-ish columns
            dt_cols = [c for c in cols if any(k in c.lower() for k in ('at', 'date', 'time', 'ts'))]
            hit = False
            for c in dt_cols:
                v = str(d.get(c, ''))
                if v and v[:10] >= cutoff.strftime('%Y-%m-%d'):
                    hit = True
            if not hit:
                continue
            print(f"    [7d-hit] {str(d)[:300]}")
    con.close()

for db in DBS:
    dump(db)
