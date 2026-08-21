# -*- coding: utf-8 -*-
import io, sys, re, glob, os

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

cands = [p for p in glob.glob("*.md") if "2026-08-06" in p]
for p in cands:
    t = open(p, encoding="utf-8").read()
    print(p, "| total:", len(t), "| han:", len(re.findall(r"[\u4e00-\u9fff]", t)),
          "| lines:", t.count("\n") + 1)
