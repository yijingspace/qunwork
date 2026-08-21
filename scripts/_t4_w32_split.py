# -*- coding: utf-8 -*-
"""t4: check test assets, rhythm doc, and plan-mapped vs external commit split."""
import subprocess, sys, io, os, glob
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# 1) test files in tests/
tests = sorted(glob.glob("tests/*.py"))
print("TEST FILES (%d):" % len(tests))
for t in tests:
    print("  ", t)

# 2) any rhythm convention doc?
for pat in ["**/*节奏*", "**/*cadence*", "**/*rhythm*"]:
    for p in glob.glob(pat, recursive=True):
        if "node_modules" not in p and ".venv" not in p and "git" not in p:
            print("RHYTHM-REL:", p)

# 3) plan-mapped commit classification (by hash prefix)
plan = {
    "G1": ["c7e946b", "79109c2", "369e282", "8c125db", "7f1a517", "f87b3fb"],
    "G2": ["0e0d8ff", "3603d42", "40dd821", "1c5634f", "f2659da", "a0b7496"],
    "G3": ["789e514", "8bd30e6", "7c98b19", "7f8aca2", "b38a368", "b8fa837", "72fb6ff"],
}
lines = subprocess.run(["git", "log", '--since=2026-08-03 00:00', '--until=2026-08-10 00:00',
                        '--pretty=format:%h|%ad|%s', '--date=format:%Y-%m-%d %H:%M', '--no-merges'],
                       capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.splitlines()
commits = [l for l in lines if l.strip() and "WIP on" not in l and "index on" not in l]
hashes = {c.split("|")[0] for c in commits}
mapped = set()
for g, hs in plan.items():
    m = [h for h in hs if h in hashes]
    mapped.update(m)
    print(f"G-mapped {g}: {len(m)}/{len(hs)}", m)
print("plan-mapped unique:", len(mapped), "of", len(commits), "=", f"{len(mapped)/len(commits)*100:.0f}%")
print("plan-external:", len(commits) - len(mapped), "=", f"{(len(commits)-len(mapped))/len(commits)*100:.0f}%")
