# -*- coding: utf-8 -*-
"""W32 复核：排除 stash 后逐日提交数；W31 净 diff 基线；08-08/08-09 提交明细。"""
import subprocess, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

def sh(args, **kw):
    r = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', errors='replace', **kw)
    return r.stdout

def daily_counts(since, until, exclude_stash=True):
    args = ["git", "log", "--since=%s" % since, "--until=%s" % until, "--all"]
    if exclude_stash:
        args.insert(3, "--exclude=refs/stash")
    out = sh(args + ["--pretty=format:%ad", "--date=format:%m-%d"])
    d = {}
    for line in out.splitlines():
        line = line.strip()
        if line:
            d[line] = d.get(line, 0) + 1
    return d

print("== W32 逐日（排除 stash）==")
d = daily_counts("2026-08-03", "2026-08-10")
for k in sorted(d):
    print("  %s: %d" % (k, d[k]))
print("  TOTAL:", sum(d.values()))

print("== 08-08 与 08-09 明细（排除 stash）==")
out = sh(["git", "log", "--exclude=refs/stash", "--all", "--since=2026-08-08", "--until=2026-08-10",
          "--pretty=format:%h|%ad|%s", "--date=format:%m-%d %H:%M"])
for line in out.splitlines():
    print("  " + line)

print("== W31 窗口边界 ==")
out = sh(["git", "log", "--all", "--pretty=format:%h|%ad|%s", "--date=format:%m-%d %H:%M",
          "--until=2026-07-27"])
print("  W30 末提交:", out.splitlines()[0] if out.strip() else "(none)")

print("== W31 净 diff（W30末 -> ed9297e）==")
w30end = out.splitlines()[0].split("|")[0] if out.strip() else "HEAD"
r = sh(["git", "diff", "--shortstat", w30end, "ed9297e"])
print("  %s -> ed9297e: %s" % (w30end, r.strip() or "(empty)"))

print("== W32 全窗口末提交（08-09 23:59 前最后一条）==")
out = sh(["git", "log", "--exclude=refs/stash", "--all", "--since=2026-08-09", "--until=2026-08-10",
          "--pretty=format:%h|%ad|%s", "--date=format:%m-%d %H:%M"])
print("  " + out.splitlines()[-1])
