# -*- coding: utf-8 -*-
"""Verify per-commit-sum figures and prev_report existence; inspect run events."""
import subprocess, re, io, sys, sqlite3, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

def run(args):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout

# 1) per-commit sum via numstat (unambiguous) for range 56b3c62..ed9297e
out = run(["git", "log", "--numstat", "--format=", "56b3c62..ed9297e"])
ins = dels = 0
for line in out.splitlines():
    parts = line.split("\t")
    if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
        ins += int(parts[0]); dels += int(parts[1])
print("numstat sum 56b3c62..ed9297e (39 commits): ins=%d dels=%d" % (ins, dels))

# 2) per-commit sum for date window 08-01..08-06 (66 commits, --all)
out = run(["git", "log", "--all", "--numstat", "--format=", "--since=2026-08-01 00:00", "--until=2026-08-07 00:00"])
ins = dels = 0
for line in out.splitlines():
    parts = line.split("\t")
    if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
        ins += int(parts[0]); dels += int(parts[1])
print("numstat sum 08-01..08-06 (66 commits): ins=%d dels=%d" % (ins, dels))

# 3) prev_report file existence (Python, encoding-safe)
for p in [r"E:\QunWork\QunWork\weekly_reports\20260802-W31_周报_测试基线.md",
          r"E:\QunWork\QunWork\weekly_reports",
          r"E:\QunWork\QunWork\组织资产健康报告_2026-08-06.md"]:
    print("exists:", os.path.exists(p), "->", p)

# 4) events for the 4 non-completed weekly-report runs
db = r"C:\Users\Administrator\AppData\Roaming\coworker\orchestration.db"
con = sqlite3.connect(db)
ids = ["orch_6ba", "orch_51e", "orch_554", "orch_ad9"]
for rid in ids:
    rows = con.execute(
        "SELECT run_id, status, intent FROM orchestration_runs WHERE run_id LIKE ?", (rid + "%",)
    ).fetchall()
    for r in rows:
        rid2 = r[0]
        evs = con.execute(
            "SELECT kind, payload FROM orchestration_events WHERE run_id=? ORDER BY seq", (rid2,)
        ).fetchall()
        kinds = {}
        for k, _ in evs:
            kinds[k] = kinds.get(k, 0) + 1
        # show error-ish payloads
        errs = [p[:180] for k, p in evs if k in ("orchestration_error", "task_failed", "error")]
        print("---", rid2, r[1], "| event kinds:", kinds)
        for e in errs[:6]:
            print("    err:", e)
con.close()
