# -*- coding: utf-8 -*-
"""t4: query real orchestration.db for runs in W32 window (08-03~08-09)."""
import os, sqlite3, sys

def main():
    ap = os.environ.get("APPDATA", "")
    db = os.path.join(ap, "coworker", "orchestration.db")
    if not os.path.exists(db):
        # fallback to workspace .state shell db
        db = os.path.join(os.getcwd(), ".state", "orchestration.db")
    print("DB:", db, "exists:", os.path.exists(db))
    con = sqlite3.connect(db)
    cur = con.cursor()
    tables = [r[0] for r in cur.execute("select name from sqlite_master where type='table'")]
    print("tables:", tables)
    for t in tables:
        cols = [c[1] for c in cur.execute(f"PRAGMA table_info({t})")]
        print(f"--- {t} cols: {cols}")
        n = cur.execute(f"select count(*) from {t}").fetchone()[0]
        print(f"    rows: {n}")
        # try to find a time-ish column and status column
        time_col = next((c for c in cols if any(k in c.lower() for k in ("time", "created", "start", "date"))), None)
        status_col = next((c for c in cols if "status" in c.lower()), None)
        id_col = cols[0]
        if time_col and status_col:
            q = f"select {id_col},{time_col},{status_col} from {t} order by {time_col}"
            for row in cur.execute(q):
                print("   ", row)
        elif time_col:
            q = f"select {id_col},{time_col} from {t} order by {time_col}"
            for row in cur.execute(q):
                print("   ", row)
    con.close()

if __name__ == "__main__":
    main()
