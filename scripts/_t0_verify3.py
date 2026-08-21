# -*- coding: utf-8 -*-
"""Close the 13,635 hypothesis + verify W32 net-diff claims."""
import subprocess, io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

def run(args):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout

# W32 per-commit sum (08-03..08-07, 27 commits)
out = run(["git", "log", "--all", "--numstat", "--format=", "--since=2026-08-03 00:00", "--until=2026-08-07 00:00"])
ins = dels = 0
for line in out.splitlines():
    p = line.split("\t")
    if len(p) == 3 and p[0].isdigit() and p[1].isdigit():
        ins += int(p[0]); dels += int(p[1])
print("W32 numstat sum (27 commits): ins=%d dels=%d  -> 8612? no: W31 8621 + W32 %d = %d ; dels 894+%d=%d" % (ins, dels, ins, 8621+ins, dels, 894+dels))

# W32 net diff claims: ed9297e..HEAD and 56b3c62..HEAD
print("W32 net  ed9297e..HEAD :", run(["git", "diff", "--shortstat", "ed9297e", "HEAD"]).strip())
print("Full net 56b3c62..HEAD :", run(["git", "diff", "--shortstat", "56b3c62", "HEAD"]).strip())

# earliest run in real DB
import sqlite3, datetime
con = sqlite3.connect(r"C:\Users\Administrator\AppData\Roaming\coworker\orchestration.db")
row = con.execute("SELECT MIN(created_at), MAX(created_at), COUNT(*) FROM orchestration_runs").fetchone()
print("DB runs: min_created=%s max_created=%s total=%d" % (
    datetime.datetime.fromtimestamp(row[0]).strftime("%Y-%m-%d %H:%M:%S") if row[0] else None,
    datetime.datetime.fromtimestamp(row[1]).strftime("%Y-%m-%d %H:%M:%S") if row[1] else None, row[2]))
# runs strictly before 07-27
pre = con.execute("SELECT COUNT(*) FROM orchestration_runs WHERE created_at < ?", (datetime.datetime(2026,7,27).timestamp(),)).fetchone()[0]
print("runs before 2026-07-27 00:00:", pre)
# all statuses full window 07-27..08-02 vs total
from collections import Counter
rows = con.execute("SELECT status, created_at FROM orchestration_runs").fetchall()
tot = Counter(r[0] for r in rows)
win = Counter(r[0] for r in rows if r[1] and datetime.datetime(2026,7,27).timestamp() <= r[1] < datetime.datetime(2026,8,3).timestamp())
print("status all-time:", dict(tot))
print("status window 07-27..08-02:", dict(win))
con.close()
