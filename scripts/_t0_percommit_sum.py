# -*- coding: utf-8 -*-
"""Sum per-commit shortstat between 56b3c62..ed9297e (explanatory only)."""
import subprocess, re, io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
out = subprocess.run(
    ["git", "log", "--shortstat", "--format=", "56b3c62..ed9297e"],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
).stdout
ins = dels = files = 0
for line in out.splitlines():
    m = re.search(r"(\d+) files? changed(?:, (\d+) insertions?\(\+\))?(?:, (\d+) deletions?\(-\))?", line)
    if m:
        files += int(m.group(1))
        ins += int(m.group(2) or 0)
        dels += int(m.group(3) or 0)
print("per-commit sum: files=%d ins=%d dels=%d" % (files, ins, dels))
