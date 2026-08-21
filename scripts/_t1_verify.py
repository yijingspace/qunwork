# -*- coding: utf-8 -*-
"""T1 verify: precise aggregates for the week 2026-08-01 .. 2026-08-06.

Computes: per-day commit counts, total commits, files changed, insertions,
deletions, category tallies (feat/fix/...), test-file touch count, and
first/last commit hashes. Prints UTF-8.
"""
import subprocess, sys, re
from collections import Counter, OrderedDict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RANGE = ["--since=2026-08-01 00:00", "--until=2026-08-06 23:59"]


def git(*args):
    return subprocess.run(["git"] + list(args), capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout


commits = git("log", *RANGE, "--pretty=format:%h|%ad|%an|%s",
              "--date=format:%Y-%m-%d %H:%M").splitlines()
print("TOTAL commits (08-01..08-06):", len(commits))

days = OrderedDict()
for line in commits:
    h, ts, an, subj = line.split("|", 3)
    d = ts[:10]
    days[d] = days.get(d, 0) + 1
print("Per-day:", dict(days))

# Authors
auth = Counter(c.split("|")[2] for c in commits)
print("Authors:", dict(auth))

# Category tallies
cat = Counter()
for line in commits:
    subj = line.split("|", 3)[3].lower()
    if subj.startswith("feat"): cat["feat"] += 1
    elif subj.startswith("fix"): cat["fix"] += 1
    elif subj.startswith("i18n"): cat["i18n"] += 1
    elif subj.startswith("ui"): cat["ui"] += 1
    elif subj.startswith("brand"): cat["brand"] += 1
    elif subj.startswith("security"): cat["security"] += 1
    elif subj.startswith("perf"): cat["perf"] += 1
    elif subj.startswith("benchmark"): cat["benchmark"] += 1
    elif subj.startswith("chore"): cat["chore"] += 1
    elif subj.startswith("dev-plan"): cat["dev-plan"] += 1
    else: cat["other"] += 1
print("Categories:", dict(cat))

# Diff totals
stat = git("log", *RANGE, "--shortstat", "--pretty=format:")
files = ins = dels = 0
for line in stat.splitlines():
    m = re.search(r"(\d+) files? changed", line)
    if m: files += int(m.group(1))
    m = re.search(r"(\d+) insertions?\(\+\)", line)
    if m: ins += int(m.group(1))
    m = re.search(r"(\d+) deletions?\(-\)", line)
    if m: dels += int(m.group(1))
print(f"Diff totals: {files} files changed, +{ins} / -{dels} lines")

# Test files touched
touched = set()
for line in git("log", *RANGE, "--pretty=format:", "--name-only").splitlines():
    line = line.strip()
    if line and ("tests/" in line or line.endswith(".test.tsx")):
        touched.add(line)
print("Test files touched:", len(touched))

# First / last commits
print("First:", commits[0])
print("Last:", commits[-1])
