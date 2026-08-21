# -*- coding: utf-8 -*-
"""t4: verify deliverable files + swarm runs in W32 window."""
import subprocess, sys, io, os, sqlite3, glob
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# 1) deliverable files existence
files = ["weekly_report_template.md", "scripts/weekly_report_run.py",
         ".qunwork/weekly/data_2026W32.json", "scripts/fpa_table.py",
         "weekly_reports/2026-W32_周报.md", "weekly_reports/2026-W31_周报.md",
         "coworker/hornet/builder.py", "coworker/hornet/resonator.py",
         "coworker/hornet/observer.py", "coworker/hornet/store.py",
         "coworker/hornet/sim3d.py", "coworker/hornet/health.py",
         "packaging/build_env.ps1", "packaging/install.ps1"]
for f in files:
    print(("EXIST " if os.path.exists(f) else "MISS  ") + f)

# 2) swarm runs in W32 window (real DB)
db_candidates = [
    os.path.expandvars(r"%APPDATA%\Roaming\coworker\orchestration.db"),
    r".state/orchestration.db",
]
for db in db_candidates:
    if os.path.exists(db):
        try:
            con = sqlite3.connect(db)
            cur = con.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in cur.fetchall()]
            print(f"\nDB {db} tables: {tables}")
            if "orchestration_runs" in tables:
                cur.execute("SELECT created_at FROM orchestration_runs")
                rows = cur.fetchall()
                print("DB run rows:", len(rows))
                w32 = [r for r in rows if r[0] and "2026-08-03" <= str(r[0])[:10] <= "2026-08-09"]
                print("W32 window runs:", len(w32))
                for r in sorted(w32):
                    print("   run:", str(r[0])[:19])
            con.close()
        except Exception as e:
            print(f"DB {db} error: {e}")
    else:
        print(f"\nDB NOT FOUND: {db}")
