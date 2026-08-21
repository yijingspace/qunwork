# -*- coding: utf-8 -*-
import re

path = r"E:\QunWork\QunWork\weekly_reports\2026-W31_周报.md"
with open(path, encoding="utf-8") as f:
    text = f.read()

print("total chars:", len(text))
print("total lines:", text.count("\n") + 1)

# 1) Section titles verbatim
titles = [
    "## 📋 一、本周工作事项与成果",
    "## 🎯 二、目标达成度评估",
    "## 🗓 三、下周计划",
    "## 附录：数据核验与口径说明",
]
for t in titles:
    print(("TITLE OK  " if t in text else "TITLE MISS"), t)

# 2) Status enums in chapter 2 (between 二、 and 三、)
m2 = re.search(r"## 🎯 二、目标达成度评估(.*?)## 🗓 三、下周计划", text, re.S)
ch2 = m2.group(1) if m2 else ""
allowed = {"✅", "🟡", "❌"}
forbidden_candidates = ["⏳", "⚠️", "🔄", "🔴", "🟢", "🔵", "⚪", "🟠", "🟣"]
found_forbidden = [c for c in forbidden_candidates if c in ch2]
print("ch2 status chars found:", sorted({c for c in ch2 if c in "✅🟡❌"}))
print("ch2 forbidden status chars:", found_forbidden or "none")
print("ch2 counts: ✅", ch2.count("✅"), "🟡", ch2.count("🟡"), "❌", ch2.count("❌"))

# 3) Key numbers present
keys = ["39", "8,270", "543", "152", "13", "18 次", "26 次", "697", "66.7%", "100%", "56b3c62", "ed9297e"]
for k in keys:
    print(("NUM OK    " if k in text else "NUM MISS  "), k)

# 4) Check no leftover wrong-window numbers (66 commits / 13,144 / 192 files / 6 次 run claims)
banned = ["66 次提交", "+13,144", "192 个文件", "192 文件", "6 次编排 run"]
for b in banned:
    print(("BANNED-ABSENT" if b not in text else "BANNED-FOUND!!"), b)

# 5) Filename sanity
import os
print("filename ok:", os.path.basename(path).startswith("2026-W31_") and "W31" in os.path.basename(path))
