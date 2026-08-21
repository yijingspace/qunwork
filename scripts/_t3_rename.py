# -*- coding: utf-8 -*-
import os, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
d = "weekly_reports"
for fn in os.listdir(d):
    if "W31" in fn and "测试基线" in fn:
        new = fn.replace("2026-W31", "20260802-W31")
        os.rename(os.path.join(d, fn), os.path.join(d, new))
        print("renamed ->", new)
print("dir:", os.listdir(d))
