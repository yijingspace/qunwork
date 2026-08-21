# -*- coding: utf-8 -*-
"""Inventory: workspaces row, any weekly-report / task-record artifacts, git stats."""
import sqlite3, glob, os, sys, subprocess

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

con = sqlite3.connect(".state/coworker.db")
con.row_factory = sqlite3.Row
for r in con.execute("select * from workspaces"):
    print("workspaces:", dict(r))
con.close()

print("\n--- candidate report/record files in repo (tracked or not) ---")
for pat in ["**/*.md", "**/*周报*", "**/*weekly*", "**/*report*", "**/*.json"]:
    for f in glob.glob(pat, recursive=True):
        if any(x in f for x in ("node_modules", ".git", "egg-info", "__pycache__")):
            continue
        print(" ", f)

print("\n--- git: files changed this week (since 2026-08-01) ---")
out = subprocess.run(
    ["git", "log", "--since=2026-08-01", "--name-only", "--pretty=format:"],
    capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
files = sorted(set(l for l in out.splitlines() if l.strip()))
print(f"{len(files)} distinct files touched:")
print("\n".join(files))
