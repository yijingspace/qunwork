# -*- coding: utf-8 -*-
"""Inspect .state/*.db schemas and row counts."""
import sqlite3, os

for f in ["automation.db", "orchestration.db", "coworker.db"]:
    path = os.path.join(".state", f)
    print("=" * 70)
    print("DB:", f, "size:", os.path.getsize(path))
    con = sqlite3.connect(path)
    tables = [t[0] for t in con.execute(
        "select name from sqlite_master where type='table'")]
    print("tables:", tables)
    for t in tables:
        try:
            cols = [c[1] for c in con.execute(f"PRAGMA table_info({t})")]
            n = con.execute(f"select count(*) from {t}").fetchone()[0]
            print(f"  -- {t} (rows={n}) cols={cols}")
        except Exception as e:
            print("  --", t, "ERR", e)
    con.close()
