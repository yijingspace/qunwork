# -*- coding: utf-8 -*-
"""t4: verify W32 commit counts / daily distribution / net diff for 目标达成度评估."""
import subprocess, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout

# 1) total commits in window (exclude stash entries)
lines = run(["git", "log", '--since=2026-08-03 00:00', '--until=2026-08-10 00:00',
             '--pretty=format:%h|%ad|%s', '--date=format:%Y-%m-%d %H:%M', '--no-merges']).splitlines()
commits = [l for l in lines if l.strip() and "WIP on" not in l and "index on" not in l]
print("TOTAL_W32_COMMITS:", len(commits))

# 2) daily distribution
from collections import Counter
days = Counter()
for c in commits:
    days[c.split("|")[1][:10]] += 1
for d in sorted(days):
    print("DAY", d, days[d])

# 3) net diff ed9297e -> fe3abd5 (W32 window last commit)
diff = run(["git", "diff", "--shortstat", "ed9297e", "fe3abd5"])
print("NET_DIFF_ed9297e_fe3abd5:", diff.strip())

# 4) confirm fe3abd5 is within window and is the last 08-09 commit
print("LAST_COMMITS:")
for c in commits[:3]:
    print("  ", c)
