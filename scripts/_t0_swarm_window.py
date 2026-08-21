# -*- coding: utf-8 -*-
"""W31 fact-table: swarm run stats for 2026-07-27 .. 2026-08-02 window.
Queries both the real AppData DB and the .state shell DB.
"""
import sqlite3, sys, datetime, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

DBS = {
    "real(APPDATA)": r"C:\Users\Administrator\AppData\Roaming\coworker\orchestration.db",
    ".state-shell": r"E:\QunWork\QunWork\.state\orchestration.db",
}

def ts(s):
    dt = datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    return dt.timestamp()

W_START = ts("2026-07-27 00:00:00")
W_END = ts("2026-08-03 00:00:00")

for name, path in DBS.items():
    print("=" * 70)
    print(f"DB: {name}  ({path})")
    try:
        con = sqlite3.connect(path)
    except Exception as e:
        print("  connect error:", e)
        continue
    cols = [r[1] for r in con.execute("PRAGMA table_info(orchestration_runs)")]
    print("  columns:", cols)
    total = con.execute("SELECT COUNT(*) FROM orchestration_runs").fetchone()[0]
    print(f"  total runs: {total}")
    try:
        rows = con.execute(
            "SELECT run_id, status, created_at, updated_at, intent FROM orchestration_runs"
        ).fetchall()
    except Exception as e:
        print("  query error:", e)
        continue
    if not rows:
        print("  (no rows)")
        continue
    # window filter
    win = [r for r in rows if r[2] is not None and W_START <= r[2] < W_END]
    print(f"  runs in window 07-27..08-02: {len(win)}")
    from collections import Counter
    st = Counter(r[1] for r in win)
    print("  status dist (window):", dict(st))
    allst = Counter(r[1] for r in rows)
    print("  status dist (all):", dict(allst))
    for r in sorted(win, key=lambda x: x[2]):
        created = datetime.datetime.fromtimestamp(r[2]).strftime("%Y-%m-%d %H:%M:%S")
        updated = datetime.datetime.fromtimestamp(r[3]).strftime("%Y-%m-%d %H:%M:%S") if r[3] else "-"
        print(f"    {r[0]} {r[1]:<12} created={created} updated={updated} intent={str(r[4])[:50]}")
    con.close()
