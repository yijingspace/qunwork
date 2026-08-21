# -*- coding: utf-8 -*-
"""t4: gather all data needed to assemble the weekly report."""
import os, sqlite3, subprocess, sys, io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

def run(cmd, cwd="."):
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        return r.stdout or ""
    except Exception as e:
        return f"ERR {e}"

print("=== A. git log since 2026-08-01 (this week) ===")
out = run(["git", "log", "--since=2026-08-01", "--pretty=format:%h|%ad|%s", "--date=short"])
print(out)

print("\n=== B. git shortlog -sn since 2026-08-01 ===")
print(run(["git", "shortlog", "-sn", "--since=2026-08-01"]))

print("\n=== C. per-day commit counts since 2026-07-30 ===")
out = run(["git", "log", "--since=2026-07-30", "--pretty=format:%ad", "--date=format:%Y-%m-%d"])
days = {}
for d in out.splitlines():
    d = d.strip()
    days[d] = days.get(d, 0) + 1
for d in sorted(days):
    print(f"{d}: {days[d]}")

print("\n=== D. weekly diff stat (insertions/deletions/files) since 2026-08-01 ===")
print(run(["git", "log", "--since=2026-08-01", "--shortstat", "--pretty=format:%h %s"]))

print("\n=== E. files in scripts/ ===")
for f in sorted(os.listdir("scripts")):
    print("scripts/" + f)

print("\n=== F. md files in workspace (excluding .git/.state/__pycache__/node_modules) ===")
for root, dirs, files in os.walk("."):
    dirs[:] = [d for d in dirs if d not in (".git", ".state", "__pycache__",
               "node_modules", ".coworker", ".qunwork", "coworker.egg-info",
               "packaging", "dist", "build", ".venv", "venv")]
    for f in files:
        if f.endswith(".md"):
            print(os.path.join(root, f))

print("\n=== G. .state tables ===")
for db in [".state/orchestration.db", ".state/coworker.db", ".state/automation.db", ".state/qunwork-8765.token"]:
    if not os.path.exists(db):
        print(db, "-> MISSING")
        continue
    if db.endswith(".token"):
        print(db, "-> token file (skipped)")
        continue
    try:
        con = sqlite3.connect(db)
        tabs = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
        print(db, "->", tabs)
        con.close()
    except Exception as e:
        print(db, "ERR", e)

print("\n=== H. recent orchestration runs ===")
try:
    con = sqlite3.connect(".state/orchestration.db")
    tabs = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    for t in tabs:
        try:
            cols = [c[1] for c in con.execute(f"pragma table_info({t})")]
            print(f"-- table {t}: {cols}")
        except Exception as e:
            print(t, "ERR", e)
    con.close()
except Exception as e:
    print("ERR", e)
