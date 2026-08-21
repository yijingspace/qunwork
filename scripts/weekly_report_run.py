# -*- coding: utf-8 -*-
"""可复用周报运行器：自动读取上周历史数据的复用逻辑。

设计（同相位模板复用）：
1. 以 today 为准计算本周区间(周一~周日)与上周区间(本周周一-7d)；
2. 从 git 统计本周/上周的提交数、逐日分布、diff 行数（作为环比基线）；
3. 在 weekly_reports/ 与 _swarm_reports/ 中查找上周报告（上周区间内 mtime 最新 .md）；
4. 抽取上周报告「三、下周计划」章节 → 本周「二、目标达成度」的自动基线；
5. 用 weekly_report_template.md 渲染成品，写入 weekly_reports/{YYYY-Www}_周报.md。

用法:
    python scripts/weekly_report_run.py                 # 用今天
    python scripts/weekly_report_run.py --date 2026-08-06
    python scripts/weekly_report_run.py --template weekly_report_template.md
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import date, timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT_DIR = os.path.join(ROOT, "weekly_reports")
SWARM_DIR = os.path.join(ROOT, "_swarm_reports")
DATA_DIR = os.path.join(ROOT, ".qunwork", "weekly")

# ---- 工具 ----------------------------------------------------------------
def git(*args):
    r = subprocess.run(["git"] + list(args), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.stdout or ""

def week_bounds(d: date):
    mon = d - timedelta(days=d.weekday())
    return mon, mon + timedelta(days=6)

def is_iso_week_contained(path, monday: date, sunday: date) -> bool:
    """文件名或 mtime 落在区间内？文件名优先（20260805-… 或 2026-08-05 / 2026-Wxx）。"""
    base = os.path.basename(path)
    m = re.search(r"(\d{4})-?(\d{2})-?(\d{2})", base)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return monday <= d <= sunday
        except ValueError:
            pass
    mt = os.path.getmtime(path)
    return monday.toordinal() <= (mt / 86400.0) + 719163 <= sunday.toordinal()

def find_prev_report(monday: date) -> str | None:
    """找上周报告：上周区间内 mtime 最新 .md。优先 weekly_reports/，其次 _swarm_reports/。"""
    prev_mon = monday - timedelta(days=7)
    prev_sun = monday - timedelta(days=1)
    cands = []
    for d in (REPORT_DIR, SWARM_DIR):
        if not os.path.isdir(d):
            continue
        for p in glob.glob(os.path.join(d, "*.md")):
            if is_iso_week_contained(p, prev_mon, prev_sun):
                cands.append((os.path.getmtime(p), p))
    if not cands:
        return None
    cands.sort()
    return cands[-1][1]

def extract_next_plan(text: str) -> str:
    """抽取「三、下周计划」章节正文（含 3.1/3.2/3.3 小节）。"""
    idx = text.find("三、下周计划")
    if idx == -1:
        idx = text.find("下周计划")
        if idx == -1:
            return ""
        idx = max(0, idx - 4)  # 回退到小节标题
    # 截到下一个二级标题(##)或文件末尾
    rest = text[idx:]
    m = re.search(r"\n##\s", rest[1:])
    return rest if not m else rest[: m.start() + 1]

def extract_goals(next_plan: str) -> list[str]:
    """从下周计划中抽取 3.1 目标行。"""
    goals = []
    m = re.search(r"3\.1[^\n]*\n(.*?)(?=\n###|\n##|\Z)", next_plan, re.S)
    if m:
        for line in m.group(1).splitlines():
            line = line.strip().lstrip("-* ").strip()
            if line and not line.startswith((">", "|", "---", "```")):
                goals.append(line)
    if not goals:
        for line in next_plan.splitlines():
            line = line.strip().lstrip("-* ").strip()
            if line.startswith(("- [", "[P0", "[P1", "[P2", "目标")):
                goals.append(line)
    return goals[:12]

def pct(a: int, b: int) -> str:
    return "—" if b == 0 else "%+d%%" % round((a - b) / b * 100)

# ---- 主流程 ----------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", type=str, default="")
    ap.add_argument("--template", type=str,
                    default=os.path.join(ROOT, "weekly_report_template.md"))
    args = ap.parse_args()

    today = date.fromisoformat(args.date) if args.date else date.today()
    mon, sun = week_bounds(today)
    prev_mon = mon - timedelta(days=7)
    prev_sun = mon - timedelta(days=1)
    ww = "W%02d" % mon.isocalendar()[1]
    yy = mon.year

    # 1. git 数据（本周 + 上周环比）
    def week_stats(d0: date):
        out = git("log", "--since=%s" % d0.isoformat(),
                  "--pretty=format:%ad|%h|%s", "--date=format:%Y-%m-%d %H:%M")
        rows, days = [], Counter()
        for line in out.splitlines():
            if "|" in line:
                ts, h, s = line.split("|", 2)
                if ts[:10] <= prev_sun.isoformat() or d0 == mon:
                    rows.append((ts[:10], h, s.strip()))
                    days[ts[:10]] += 1
        st = git("log", "--since=%s" % d0.isoformat(), "--shortstat", "--pretty=format:")
        ins = dels = 0
        for line in st.splitlines():
            m = re.search(r"(\d+) insertions?\(\+\)", line)
            if m: ins += int(m.group(1))
            m = re.search(r"(\d+) deletions?\(-\)", line)
            if m: dels += int(m.group(1))
        return rows, dict(sorted(days.items())), ins, dels

    rows, per_day, ins, dels = week_stats(mon)
    lrows, lper_day, lins, ldels = week_stats(prev_mon)

    # 2. 查找并解析上周报告（复用逻辑核心）
    prev_report = find_prev_report(mon)
    prev_plan, goals = "", []
    if prev_report:
        with open(prev_report, encoding="utf-8", errors="replace") as f:
            prev_plan = extract_next_plan(f.read())
        goals = extract_goals(prev_plan)
    else:
        prev_report = "(未找到上周报告 — 基线留空，不阻断生成)"

    # 3. 渲染模板
    with open(args.template, encoding="utf-8") as f:
        tpl = f.read()

    goal_rows = "\n".join(
        "| %s | P0 | 待评估 | — | — |" % g for g in (goals or ["(无上周基线，本次为首次运行)"])
    ).strip()
    fill = {
        "this_commits": str(len(rows)),
        "last_commits": str(len(lrows)),
        "delta_commits": pct(len(rows), len(lrows)),
        "this_ins": str(ins),
        "this_dels": str(dels),
        "last_ins": str(lins),
        "last_dels": str(ldels),
        "milestone": "—",
        "prev_report": os.path.relpath(prev_report, ROOT) if os.path.isfile(prev_report) else prev_report,
        "goal_rows": goal_rows,
        "today": today.isoformat(),
        "week_label": "%s-%s" % (mon.isoformat(), sun.isoformat()),
    }
    out = tpl
    for k, v in fill.items():
        out = out.replace("{" + k + "}", v)

    # 4. 写入周报
    os.makedirs(REPORT_DIR, exist_ok=True)
    target = os.path.join(REPORT_DIR, "%d-%s_周报.md" % (yy, ww))
    with open(target, "w", encoding="utf-8") as f:
        f.write(out)

    # 5. 数据快照（供其他章节/后续分析复用）
    os.makedirs(DATA_DIR, exist_ok=True)
    snapshot = {
        "generated_at": today.isoformat(),
        "week": {"monday": mon.isoformat(), "sunday": sun.isoformat()},
        "this_week": {"commits": len(rows), "per_day": per_day, "ins": ins, "dels": dels},
        "last_week": {"commits": len(lrows), "per_day": lper_day, "ins": lins, "dels": ldels},
        "prev_report": prev_report,
        "reused_goals": goals,
    }
    with open(os.path.join(DATA_DIR, "data_%d_%s.json" % (yy, ww)), "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    print("✅ 周报已生成: %s" % os.path.relpath(target, ROOT))
    print("   本周 %s~%s: %d commits (+%d/-%d)" % (mon, sun, len(rows), ins, dels))
    print("   上周 %s~%s: %d commits (+%d/-%d)" % (prev_mon, prev_sun, len(lrows), lins, ldels))
    print("   复用上周报告: %s" % prev_report)
    print("   抽取上周目标 %d 条 → 已填入达成度基线" % len(goals))

if __name__ == "__main__":
    main()
