# -*- coding: utf-8 -*-
"""Search coworker/ for scheduler prior-result reuse machinery (T5)."""
import os, re, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

root = "coworker"
pats = ["prior_result", "prior-result", "phase_slot", "reuse", "weekly", "periodic phase", "scheduled"]
hits = {}
for dirpath, dirnames, filenames in os.walk(root):
    if any(x in dirpath for x in ["egg-info", "site-packages", "target", "__pycache__"]):
        continue
    for fn in filenames:
        if not fn.endswith(".py"):
            continue
        p = os.path.join(dirpath, fn)
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    if any(pat in line for pat in pats):
                        hits.setdefault(p, []).append((i, line.rstrip()))
        except Exception:
            pass

for p in sorted(hits):
    print("== %s" % p)
    for i, line in hits[p][:12]:
        print("  %4d: %s" % (i, line.strip()[:150]))
    print()
