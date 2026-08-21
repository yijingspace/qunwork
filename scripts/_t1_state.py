# -*- coding: utf-8 -*-
"""Dump orchestration_runs + events + automation/coworker state for runs_last7d."""
import sqlite3, json, os

con = sqlite3.connect(os.path.join(".state", "orchestration.db"))
con.row_factory = sqlite3.Row
print("### orchestration_runs ###")
for r in con.execute("SELECT run_id,intent,status,created_at,updated_at,length(final) AS f FROM orchestration_runs ORDER BY created_at"):
    print(json.dumps(dict(r), ensure_ascii=False))
print()
print("### orchestration_events (kind counts per run) ###")
for r in con.execute("""SELECT run_id, kind, COUNT(*) n FROM orchestration_events GROUP BY run_id, kind ORDER BY run_id, kind"""):
    print(dict(r))
print()
print("### orchestration_events rows ###")
for r in con.execute("SELECT run_id, seq, kind, substr(payload,1,300) p, ts FROM orchestration_events ORDER BY id LIMIT 400"):
    d = dict(r)
    print(json.dumps(d, ensure_ascii=False))
con.close()

print()
print("### automation.db ###")
con = sqlite3.connect(os.path.join(".state", "automation.db"))
con.row_factory = sqlite3.Row
for t in ["scheduled_tasks", "task_runs"]:
    for r in con.execute(f"SELECT * FROM {t}"):
        print(t, json.dumps(dict(r), ensure_ascii=False))
con.close()

print()
print("### coworker.db audit_events (recent) ###")
con = sqlite3.connect(os.path.join(".state", "coworker.db"))
con.row_factory = sqlite3.Row
for r in con.execute("SELECT id,timestamp,session_id,agent,tool,stage,status,substr(resource,1,80) res FROM audit_events ORDER BY id DESC LIMIT 30"):
    print(json.dumps(dict(r), ensure_ascii=False))
print("audit count:", con.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0])
print("sessions:", con.execute("SELECT session_id, title, agent, n_msgs, updated_at FROM sessions ORDER BY updated_at DESC LIMIT 20").fetchall() and [dict(x) for x in con.execute("SELECT session_id, title, agent, n_msgs, updated_at FROM sessions ORDER BY updated_at DESC LIMIT 20")])
con.close()
