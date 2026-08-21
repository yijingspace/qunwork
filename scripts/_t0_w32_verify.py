# -*- coding: utf-8 -*-
"""W32 周报 1.2 关键数据核验：逐日提交数、窗口净 diff、里程碑 commit 存在性。"""
import subprocess, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

def sh(args, **kw):
    r = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', errors='replace', **kw)
    return r.stdout

def daily_counts(since, until):
    out = sh(["git", "log", "--since=%s" % since, "--until=%s" % until, "--all",
              "--pretty=format:%ad", "--date=format:%m-%d"])
    d = {}
    for line in out.splitlines():
        line = line.strip()
        if line:
            d[line] = d.get(line, 0) + 1
    return d

def shortstat(a, b):
    out = sh(["git", "diff", "--shortstat", a, b])
    return out.strip() or "(empty)"

print("== W32 window 08-03..08-09 (since=2026-08-03 until=2026-08-10) ==")
d32 = daily_counts("2026-08-03", "2026-08-10")
for k in sorted(d32):
    print("  %s: %d" % (k, d32[k]))
print("  TOTAL:", sum(d32.values()))

print("== W31 window 07-27..08-02 (since=2026-07-27 until=2026-08-03) ==")
d31 = daily_counts("2026-07-27", "2026-08-03")
for k in sorted(d31):
    print("  %s: %d" % (k, d31[k]))
print("  TOTAL:", sum(d31.values()))

print("== net diff (环比口径, 净 diff) ==")
print("  W32  ed9297e -> d149dc2 :", shortstat("ed9297e", "d149dc2"))
print("  W31  base -> ed9297e   :", shortstat("ed9297e^", "ed9297e"))

print("== 关键 commit 存在性 ==")
keys = ["f6a57da","c5121cb","d1ff87a","4d14758","ce87c49","a229178","2f3c1f4","f2659da",
        "dbe8536","d3665ff","bd868ed","91eb3bf","f642924","28e302c","1a2e20c","d149dc2",
        "0bb608e","8a5cd35","fe3abd5","7ba5c40","50af728","983fc41","5489cd8","6ed6f9b",
        "bb9347a","bdb2c24"]
for k in keys:
    out = sh(["git", "cat-file", "-t", k]).strip()
    print("  %s: %s" % (k, out))
