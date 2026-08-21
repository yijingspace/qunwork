# -*- coding: utf-8 -*-
"""Aggregate last-7-days commits by day: count / authors / subjects."""
import subprocess
import collections

out = subprocess.run(
    ["git", "log", "--since=7.days", "--pretty=format:%ad|%an|%s", "--date=format:%Y-%m-%d"],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
).stdout

rows = [line.split("|", 2) for line in out.splitlines() if line.strip()]
by_day = collections.OrderedDict()
for day, author, subject in rows:
    by_day.setdefault(day, []).append((author, subject))

total = 0
for day in sorted(by_day):
    items = by_day[day]
    total += len(items)
    authors = collections.Counter(a for a, _ in items)
    print(f"{day}: {len(items)} commits | authors: {dict(authors)}")

print(f"TOTAL: {total}")
print("ALL AUTHORS:", collections.Counter(a for _, a, _ in rows))
