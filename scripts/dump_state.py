# -*- coding: utf-8 -*-
"""Dump schema + row counts of .state / .qunwork SQLite DBs (for weekly inventory)."""
import sqlite3, glob, os, sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

for f in sorted(glob.glob(".state/*.db") + glob.glob(".qunwork/*.db")):
    print("=" * 70)
    print("DB:", f, os.path.getsize(f), "bytes")
    con = sqlite3.connect(f)
    con.row_factory = sqlite3.Row
    tables = [r[0] for r in con.execute(
        "select name from sqlite_master where type='table' order by name")]
    for t in tables:
        try:
            n = con.execute(f'select count(*) from "{t}"').fetchone()[0]
        except Exception as e:
            n = f"ERR {e}"
        cols = [c[1] for c in con.execute(f'PRAGMA table_info("{t}")')]
        print(f"  - {t}  (rows={n})  cols={cols}")
    con.close()
