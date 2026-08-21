# -*- coding: utf-8 -*-
"""Full run+event dump from the app's orchestration store (path used by scripts/swarm_history.py)."""
import sqlite3, json, os, datetime
from pathlib import Path

db = str(Path.home() / "AppData/Roaming/coworker/orchestration.db")
print("DB:", db, "exists:", os.path.exists(db))
if not os.path.exists(db):
    raise SystemExit(1)
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row

print("### orchestration_runs ###")
for r in con.execute("SELECT run_id,intent,status,created_at,updated_at,length(final) f FROM orchestration_runs ORDER BY created_at"):
    d = dict(r)
    d["created_iso"] = datetime.datetime.fromtimestamp(d["created_at"]).isoformat(timespec="seconds")
    d["updated_iso"] = datetime.datetime.fromtimestamp(d["updated_at"]).isoformat(timespec="seconds")
    print(json.dumps(d, ensure_ascii=False))
print()
print("### events per run (seq, kind, payload-head) ###")
for r in con.execute("SELECT run_id, seq, kind, substr(payload,1,260) p, ts FROM orchestration_events ORDER BY run_id, seq"):
    d = dict(r)
    if d.get("ts"):
        d["ts_iso"] = datetime.datetime.fromtimestamp(d["ts"]).isoformat(timespec="seconds")
    print(json.dumps(d, ensure_ascii=False))
con.close()
