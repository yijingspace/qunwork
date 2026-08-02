"""Swarm run comparison — quantify panel vs main-session paths from run_store.

Usage:
    python scripts/swarm_history.py [--last N] [--db PATH]

Shows each orchestration run's intent, status, wall-clock duration, task count,
dispatch/retry counts and governance actions — enough to compare the panel path
vs the main-session path on the same probe (e.g. N=14 dual-file test).
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", type=int, default=6)
    ap.add_argument("--db", default=str(Path.home() / "AppData/Roaming/coworker/orchestration.db"))
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    runs = con.execute(
        "SELECT run_id, intent, status, created_at, updated_at, final "
        "FROM orchestration_runs ORDER BY created_at DESC LIMIT ?",
        (args.last,),
    ).fetchall()

    print(f"{'run':<10} {'dur(s)':>7} {'st':<10} {'#tasks':>6} {'#exec':>6} {'#gov':>5}  intent")
    for run_id, intent, status, created, updated, final in runs:
        ev = con.execute(
            "SELECT kind, payload FROM orchestration_events WHERE run_id=? ORDER BY seq", (run_id,)
        ).fetchall()
        n_task_start = sum(1 for k, _ in ev if k == "task_started")
        n_exec = sum(1 for k, _ in ev if k == "task_done")
        n_gov = sum(1 for k, _ in ev if k == "governance")
        dur = round(updated - created, 1) if updated and created else -1
        print(
            f"{run_id[:8]:<10} {dur:>7} {status:<10} {n_task_start:>6} {n_exec:>6} {n_gov:>5}  {intent[:42]}"
        )


if __name__ == "__main__":
    main()
