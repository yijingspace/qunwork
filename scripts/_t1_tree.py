# -*- coding: utf-8 -*-
"""List swarm report + weekly report files with mtime, and .qunwork tree."""
import os, datetime

for base in ["_swarm_reports", "weekly_reports", ".qunwork", ".coworker"]:
    print("=" * 70)
    print("DIR:", base)
    for root, dirs, files in os.walk(base):
        for f in sorted(files):
            p = os.path.join(root, f)
            mt = datetime.datetime.fromtimestamp(os.path.getmtime(p))
            print(f"  {mt:%Y-%m-%d %H:%M:%S}  {os.path.getsize(p):>7}  {p}")
