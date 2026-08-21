# -*- coding: utf-8 -*-
"""提取关键提交涉及文件（top 路径），供产出物清单使用。"""
import subprocess, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

def sh(args):
    r = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', errors='replace')
    return r.stdout

for c in ["d149dc2", "0bb608e", "8a5cd35", "fe3abd5", "d3665ff", "bd868ed", "ce87c49",
          "f6a57da", "c5121cb", "d1ff87a", "7ba5c40", "f642924", "2f3c1f4", "dbe8536"]:
    out = sh(["git", "show", "--stat", "--format=%h %s", c])
    lines = [l for l in out.splitlines() if l.strip() and not l.startswith(" ") and "|" not in l]
    print("=== %s" % lines[0] if lines else "=== %s (no files?)" % c)
    for l in out.splitlines():
        if "|" in l and "file" not in l:
            print("   " + l.strip()[:110])
