# -*- coding: utf-8 -*-
"""Per-day commit counts + diff stats for the week."""
import subprocess, sys
from collections import Counter, OrderedDict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

out = subprocess.run(
    ["git", "log", "--since=2026-08-01", "--pretty=format:%ad|%h|%s",
     "--date=format:%Y-%m-%d %H:%M"],
    capture_output=True, text=True, encoding="utf-8", errors="replace").stdout

days = Counter()
rows = []
for line in out.splitlines():
    if not line.strip():
        continue
    ts, h, subj = line.split("|", 2)
    days[ts[:10]] += 1
    rows.append((ts, h, subj))

print("Per-day commit counts (2026-08-01 .. 2026-08-05):")
for d in sorted(days):
    print(f"  {d}: {days[d]} commits")
print("TOTAL:", sum(days.values()))

stat = subprocess.run(
    ["git", "log", "--since=2026-08-01", "--shortstat", "--pretty=format:"],
    capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
import re
ins = dels = 0
for line in stat.splitlines():
    m = re.search(r"(\d+) insertions?\([+-]\)", line)
    if m: ins += int(m.group(1))
    m = re.search(r"(\d+) deletions?\([+-]\)", line)
    if m: dels += int(m.group(1))
print(f"\nDiff stats: +{ins} / -{dels} lines across the week")
