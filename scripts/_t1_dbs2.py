# -*- coding: utf-8 -*-
"""Inspect remaining dbs + weekly json contents."""
import sqlite3, os, json

for f in [r".coworker\knowledge.db", r".qunwork\memory.db"]:
    print("=" * 70)
    print("DB:", f, "size:", os.path.getsize(f))
    con = sqlite3.connect(f)
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

print("=" * 70)
for jf in [r".qunwork\weekly\data_2026W32.json", r".qunwork\weekly\data_2026_W32.json"]:
    print("JSON:", jf, "size:", os.path.getsize(jf))
    with open(jf, encoding="utf-8") as fh:
        data = json.load(fh)
    print(json.dumps(data, ensure_ascii=False, indent=1)[:4000])
