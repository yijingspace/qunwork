# -*- coding: utf-8 -*-
"""t3: verify W32 git window commits & per-day distribution."""
import subprocess, sys, io, json
from collections import OrderedDict
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

def run(args):
    r = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', errors='replace')
    return r.stdout.strip()

# W32 window: since=2026-08-03 until=2026-08-10
out = run(['git','log','--since=2026-08-03','--until=2026-08-10',
           '--pretty=format:%ad|%s','--date=format:%Y-%m-%d'])
lines = [l for l in out.splitlines() if l.strip()]
per_day = OrderedDict()
subjects = []
for l in lines:
    d, s = l.split('|', 1)
    per_day[d] = per_day.get(d, 0) + 1
    subjects.append(s)
total = len(lines)
print("== W32 window total commits:", total)
for d, c in per_day.items():
    print(f"  {d}: {c}")
# full commit list (short hashes) for cross-check
out2 = run(['git','log','--since=2026-08-03','--until=2026-08-10','--pretty=format:%h %ad %s','--date=format:%Y-%m-%d'])
print("\n== Full list (hash date subject):")
for l in out2.splitlines():
    print(" ", l)
print("\n== W32 subjects (JSON-ready):")
print(json.dumps(subjects, ensure_ascii=False, indent=0))
