# -*- coding: utf-8 -*-
"""t4: convert epoch timestamps and count W32 window runs."""
import sys, io, os, sqlite3, datetime
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

db = os.path.join(os.environ.get("APPDATA", ""), "coworker", "orchestration.db")
con = sqlite3.connect(db)
cur = con.cursor()
cur.execute("SELECT run_id, intent, status, created_at, updated_at FROM orchestration_runs ORDER BY created_at")
rows = cur.fetchall()
def dt(ts):
    try:
        return datetime.datetime.fromtimestamp(float(ts))
    except Exception:
        return None
w32 = []
for rid, intent, status, ca, ua in rows:
    d = dt(ca)
    ds = d.strftime("%Y-%m-%d %H:%M:%S") if d else str(ca)
    inw32 = d and datetime.datetime(2026,8,3) <= d < datetime.datetime(2026,8,10)
    if inw32:
        w32.append((ds, status, intent, rid))
    print(("W32*" if inw32 else "    "), ds, "|", status, "|", str(intent)[:60], "|", str(rid)[:12])
print("\nW32 window total:", len(w32))
from collections import Counter
print("W32 status:", dict(Counter(s for _, s, _, _ in w32)))
con.close()
