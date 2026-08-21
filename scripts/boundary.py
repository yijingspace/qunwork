# -*- coding: utf-8 -*-
"""Week boundary + exact diff stats + coordination-report sample."""
import subprocess, sys, re, os, glob

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

def run(args):
    return subprocess.run(args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout

print("--- last commit BEFORE this week's burst (boundary) ---")
print(run(["git", "log", "a4ad35d~1", "-1",
           "--pretty=format:%h|%ad|%s", "--date=format:%Y-%m-%d %H:%M"]))

print("\n--- exact week diff stats: a4ad35d^..HEAD ---")
stat = run(["git", "log", "a4ad35d^..HEAD", "--shortstat", "--pretty=format:"])
ins = dels = 0
for line in stat.splitlines():
    m = re.search(r"(\d+) insertions?\([+-]\)", line)
    if m: ins += int(m.group(1))
    m = re.search(r"(\d+) deletions?\([+-]\)", line)
    if m: dels += int(m.group(1))
print(f"+{ins} / -{dels} lines, 63 commits")

print("\n--- orchestration run record (coordination report artifact) ---")
for f in glob.glob("surfaces/gui/src-tauri/target/**/coordination-report-*.md", recursive=True):
    print("FILE:", f)
    with open(f, encoding="utf-8", errors="replace") as fh:
        print(fh.read()[:1500])
