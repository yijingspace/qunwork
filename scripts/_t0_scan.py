# -*- coding: utf-8 -*-
"""Final scan: W31 report artifacts anywhere in repo + docs/weekly/history."""
import os, io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
root = r"E:\QunWork\QunWork"

hits = []
for base, dirs, files in os.walk(root):
    # skip heavy dirs
    dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", "__pycache__", "target", "dist", "binaries")]
    for f in files:
        if "W31" in f or "20260802" in f or "测试基线" in f:
            hits.append(os.path.relpath(os.path.join(base, f), root))
print("W31/20260802/测试基线 file hits in repo:", hits if hits else "(none)")

for p in [r"docs\weekly\history", r"docs\weekly", r"weekly_reports", r"_swarm_reports"]:
    full = os.path.join(root, p)
    if os.path.isdir(full):
        print("DIR exists:", p, "->", sorted(os.listdir(full)))
    else:
        print("DIR missing:", p)
