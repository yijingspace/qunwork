# -*- coding: utf-8 -*-
import os, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
d = "weekly_reports"
for fn in os.listdir(d):
    if "测试基线" in fn:
        os.remove(os.path.join(d, fn))
        print("removed:", fn)
print("dir:", os.listdir(d))
