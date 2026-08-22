import sqlite3, os

db = sqlite3.connect(r'C:\Users\Administrator\AppData\Roaming\coworker\hornet.db')
tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print("hornet tables:", tables)
print("Nodes:", db.execute("SELECT COUNT(*) FROM hornet_nodes").fetchone()[0])
print("Edges:", db.execute("SELECT COUNT(*) FROM hornet_edges").fetchone()[0])

rows = db.execute("SELECT id, title FROM hornet_nodes WHERE title LIKE '%graph%' OR title LIKE '%Graph%'").fetchall()
print(f"\nNodes with 'graph' in title: {len(rows)}")
for r in rows[:20]:
    eid = db.execute("SELECT COUNT(*) FROM hornet_edges WHERE src=? OR dst=?", (r[0], r[0])).fetchone()[0]
    print(f"  id={r[0]} edges={eid} title={r[1][:80]}")

print("\nSample node titles:")
for r in db.execute("SELECT id, title FROM hornet_nodes LIMIT 15").fetchall():
    print(f"  id={r[0]} {r[1][:80]}")
db.close()

# Check isolated
db = sqlite3.connect(r'C:\Users\Administrator\AppData\Roaming\coworker\hornet.db')
isolated = db.execute("""
    SELECT n.id, n.title FROM hornet_nodes n
    WHERE n.id NOT IN (SELECT src FROM hornet_edges) AND n.id NOT IN (SELECT dst FROM hornet_edges)
""").fetchall()
print(f"\nIsolated nodes: {len(isolated)}")
for r in isolated[:20]:
    print(f"  id={r[0]} {r[1][:80]}")
db.close()

# knowledge_supplements
supp = r'C:\Users\Administrator\AppData\Roaming\coworker\knowledge_supplements'
print(f"\nknowledge_supplements dir exists: {os.path.exists(supp)}")
if os.path.exists(supp):
    for f in os.listdir(supp):
        print(f"  {f}")

# knowledge.db
db2 = sqlite3.connect(r'C:\Users\Administrator\AppData\Roaming\coworker\knowledge.db')
tables2 = [r[0] for r in db2.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print(f"\nknowledge.db tables: {tables2}")
db2.close()
