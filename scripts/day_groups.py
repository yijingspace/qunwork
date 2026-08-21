# -*- coding: utf-8 -*-
"""Definitive per-day grouping of commits 2026-07-25 .. now."""
import subprocess, sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

out = subprocess.run(
    ["git", "log", "--since=2026-07-25", "--pretty=format:%ad|%h|%s",
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

total = 0
for d in sorted(days):
    print(f"{d}  {days[d]:>2} commits")
    total += days[d]
print("TOTAL since 07-25:", total)
print()
print("--- full commit list (newest first) ---")
for ts, h, subj in rows:
    print(f"{ts}  {h}  {subj}")
