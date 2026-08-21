# -*- coding: utf-8 -*-
"""T2: dump .qunwork/memory.db fully."""
import sqlite3, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
con = sqlite3.connect('.qunwork/memory.db')
con.row_factory = sqlite3.Row
tabs = [r[0] for r in con.execute("select name from sqlite_master where type='table' order by name")]
print("tables:", tabs)
for t in tabs:
    print(f"\n-- {t}")
    for row in con.execute(f'select * from "{t}"'):
        d = dict(row)
        for k, v in d.items():
            s = str(v)
            if len(s) > 200:
                s = s[:200] + f"...<len={len(str(v))}>"
            print(f"   {k} = {s}")
        print("   ---")
con.close()
