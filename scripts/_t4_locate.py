# -*- coding: utf-8 -*-
path = r"E:\QunWork\QunWork\weekly_reports\2026-W31_周报.md"
with open(path, encoding="utf-8") as f:
    lines = f.readlines()

# locate section boundaries (1-based line numbers)
marks = {}
for i, ln in enumerate(lines, 1):
    if "## 📋 一、" in ln:
        marks["ch1"] = i
    elif "## 🎯 二、" in ln:
        marks["ch2"] = i
    elif "## 🗓 三、" in ln:
        marks["ch3"] = i
    elif "## 附录" in ln:
        marks["appendix"] = i
print(marks)

def in_appendix(n):
    return marks.get("appendix", 10**9) <= n

for i, ln in enumerate(lines, 1):
    if "+13,144" in ln or "192 文件" in ln or "192 个文件" in ln:
        print(f"line {i} (appendix={in_appendix(i)}): {ln.strip()[:90]}")
