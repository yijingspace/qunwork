# -*- coding: utf-8 -*-
import sqlite3, os
for db in [r".state/orchestration.db",
           os.path.join(os.environ.get("APPDATA", ""), "coworker", "orchestration.db")]:
    print("=" * 40)
    print("DB:", db, "exists:", os.path.exists(db))
    if not os.path.exists(db):
        continue
    c = sqlite3.connect(db)
    tables = [r[0] for r in c.execute("select name from sqlite_master where type='table'")]
    print("tables:", tables)
    for t in tables:
        print("  ", t, "rows:", c.execute("select count(*) from " + t).fetchone()[0])
    c.close()
