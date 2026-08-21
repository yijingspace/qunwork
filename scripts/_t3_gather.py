# -*- coding: utf-8 -*-
"""T3: gather git data for this week + last week boundaries (reuse logic demo)."""
import subprocess, sys, re, json, os
from collections import Counter, defaultdict
from datetime import date, timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

def git(*args):
    r = subprocess.run(['git'] + list(args), capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    return r.stdout or ''

def week_bounds(ref_date: date):
    """Return (monday, sunday) of the ISO week containing ref_date."""
    monday = ref_date - timedelta(days=ref_date.weekday())
    return monday, monday + timedelta(days=6)

def git_since(d):
    return git('log', '--since=%s' % d.isoformat(),
               '--pretty=format:%ad|%h|%s', '--date=format:%Y-%m-%d %H:%M')

def diff_stats(d):
    out = git('log', '--since=%s' % d.isoformat(), '--shortstat', '--pretty=format:')
    ins = dels = 0
    for line in out.splitlines():
        m = re.search(r'(\d+) insertions?\(\+\)', line)
        if m: ins += int(m.group(1))
        m = re.search(r'(\d+) deletions?\(-\)', line)
        if m: dels += int(m.group(1))
    return ins, dels

today = date(2026, 8, 6)
mon, sun = week_bounds(today)
last_mon = mon - timedelta(days=7)
print("This week  :", mon, "->", sun)
print("Last week  :", last_mon, "->", mon - timedelta(days=1))
print()

# ---- This week commits ----
this_week = git_since(mon)
rows = []
for line in this_week.splitlines():
    if '|' in line:
        ts, h, subj = line.split('|', 2)
        rows.append((ts, h, subj.strip()))
print("=== THIS WEEK commits (%d) ===" % len(rows))
per_day = Counter(ts[:10] for ts, _, _ in rows)
for d in sorted(per_day): print("  %s: %d" % (d, per_day[d]))
print()

# ---- Last week commits (same phase, previous cycle) ----
last_week = git_since(last_mon)
lrows = []
for line in last_week.splitlines():
    if '|' in line:
        ts, h, subj = line.split('|', 2)
        if ts[:10] <= (mon - timedelta(days=1)).isoformat():
            lrows.append((ts, h, subj.strip()))
print("=== LAST WEEK commits (%d) ===" % len(lrows))
lper_day = Counter(ts[:10] for ts, _, _ in lrows)
for d in sorted(lper_day): print("  %s: %d" % (d, lper_day[d]))
print()

ins, dels = diff_stats(mon)
lins, ldels = diff_stats(last_mon)
print("=== diff stats ===")
print("this week: +%d / -%d" % (ins, dels))
print("last week: +%d / -%d" % (lins, ldels))
print()

# ---- save machine-readable JSON for reuse logic ----
os.makedirs('.qunwork/weekly', exist_ok=True)
payload = {
    "generated_at": today.isoformat(),
    "week": {"monday": mon.isoformat(), "sunday": sun.isoformat()},
    "prev_week": {"monday": last_mon.isoformat(),
                  "sunday": (mon - timedelta(days=1)).isoformat()},
    "this_week": {"commits": len(rows), "per_day": dict(sorted(per_day.items())),
                  "ins": ins, "dels": dels,
                  "subjects": [s for _, _, s in rows]},
    "last_week": {"commits": len(lrows), "per_day": dict(sorted(lper_day.items())),
                  "ins": lins, "dels": ldels,
                  "subjects": [s for _, _, s in lrows]},
}
with open('.qunwork/weekly/data_2026W32.json', 'w', encoding='utf-8') as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)
print("saved .qunwork/weekly/data_2026W32.json")

print()
print("=== THIS WEEK subjects ===")
for ts, h, s in rows: print("  %s %s %s" % (ts, h, s))
print()
print("=== LAST WEEK subjects ===")
for ts, h, s in lrows: print("  %s %s %s" % (ts, h, s))
