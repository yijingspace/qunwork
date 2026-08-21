# -*- coding: utf-8 -*-
"""t4: dump W32 commits grouped by date for rhythm evidence."""
import subprocess, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

lines = subprocess.run(["git", "log", '--since=2026-08-03 00:00', '--until=2026-08-10 00:00',
                        '--pretty=format:%h|%ad|%s', '--date=format:%Y-%m-%d %H:%M', '--no-merges'],
                       capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.splitlines()
commits = [l for l in lines if l.strip() and "WIP on" not in l and "index on" not in l]

from collections import defaultdict
byday = defaultdict(list)
for c in commits:
    h, dt, s = c.split("|", 2)
    byday[dt[:10]].append((h, dt[11:], s))

for d in sorted(byday):
    print(f"===== {d} ({len(byday[d])} commits) =====")
    for h, t, s in byday[d]:
        print(f"  {h} {t} {s[:110]}")
