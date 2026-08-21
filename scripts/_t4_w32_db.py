# -*- coding: utf-8 -*-
"""t4: fix APPDATA path + locate fpa tools."""
import subprocess, sys, io, os, sqlite3, glob
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

appdata = os.environ.get("APPDATA", "")
print("APPDATA:", appdata)
cands = [
    os.path.join(appdata, "coworker", "orchestration.db"),
    os.path.join(os.path.dirname(appdata), "coworker", "orchestration.db"),
]
for db in cands:
    if os.path.exists(db):
        con = sqlite3.connect(db)
        cur = con.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        print(f"\nDB FOUND: {db}  size={os.path.getsize(db)}  tables={tables}")
        if "orchestration_runs" in tables:
            cur.execute("SELECT created_at, status FROM orchestration_runs")
            rows = cur.fetchall()
            print("total runs:", len(rows))
            w32 = [r for r in rows if r[0] and "2026-08-03" <= str(r[0])[:10] <= "2026-08-09"]
            print("W32 window runs:", len(w32))
            for r in sorted(w32, key=lambda x: str(x[0])):
                print("   ", str(r[0])[:19], "|", r[1])
        con.close()
        break

# locate fpa / pisano tools
for pat in ["**/fpa*.py", "**/*pisano*.py", "**/*periodic*.py"]:
    for p in glob.glob(pat, recursive=True):
        if "node_modules" not in p and ".venv" not in p:
            print("FOUND:", p)
