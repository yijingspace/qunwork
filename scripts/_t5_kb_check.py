# -*- coding: utf-8 -*-
"""t5: check knowledge.db / memory.db table row counts + orchestration db sample."""
import sqlite3, io, sys, os

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

def dump(dbpath):
    if not os.path.exists(dbpath):
        print(dbpath, "-> MISSING")
        return
    con = sqlite3.connect(dbpath)
    tabs = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    print(dbpath, "tables:", tabs)
    for t in tabs:
        try:
            print("  ", t, "=", con.execute("select count(*) from " + t).fetchone()[0])
        except Exception as e:
            print("  ", t, "ERR", e)
    con.close()

for p in [".coworker/knowledge.db", ".qunwork/memory.db",
          ".state/orchestration.db", ".state/coworker.db", ".state/automation.db"]:
    dump(p)
