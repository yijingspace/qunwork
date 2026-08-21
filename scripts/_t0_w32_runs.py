"""W32 window (2026-08-03 00:00 ~ 2026-08-10 00:00) run records from the real orchestration DB."""
import sqlite3
from datetime import datetime
from pathlib import Path

db = str(Path.home() / "AppData/Roaming/coworker/orchestration.db")
con = sqlite3.connect(db)
rows = con.execute(
    "SELECT run_id, intent, status, created_at, updated_at FROM orchestration_runs ORDER BY created_at"
).fetchall()

W_START = datetime(2026, 8, 3, 0, 0, 0)
W_END = datetime(2026, 8, 10, 0, 0, 0)


def to_dt(v):
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


in_window = []
for run_id, intent, status, created, updated in rows:
    dt = to_dt(created)
    if W_START <= dt < W_END:
        dur = round(updated - created, 1) if isinstance(created, (int, float)) and isinstance(updated, (int, float)) else -1
        in_window.append((dt.strftime("%m-%d %H:%M"), run_id, status, dur, (intent or "")[:40]))

print(f"window runs: {len(in_window)}")
for r in sorted(in_window):
    print("|".join(str(x) for x in r))

from collections import Counter

print("status:", dict(Counter(r[2] for r in in_window)))
total = len(rows)
print(f"total rows in DB: {total}; earliest: {to_dt(rows[0][3]) if rows else None}")
