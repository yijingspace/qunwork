import sqlite3

# Check hornet.db
db = sqlite3.connect(r'C:\Users\Administrator\AppData\Roaming\coworker\hornet.db')
tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print(f"hornet.db tables: {tables}")
print(f"hornet_nodes count: {db.execute('SELECT COUNT(*) FROM hornet_nodes').fetchone()[0]}")
print(f"hornet_edges count: {db.execute('SELECT COUNT(*) FROM hornet_edges').fetchone()[0]}")

# Check if any node has 'graph' in title or content
rows = db.execute("SELECT id, title FROM hornet_nodes WHERE title LIKE '%graph%' OR title LIKE '%Graph%' OR title LIKE '%GRAPH%'").fetchall()
print(f"\nNodes with 'graph' in title: {len(rows)}")
for r in rows[:10]:
    print(f"  {r}")

# Show some sample titles
print("\n--- Sample node titles (first 20) ---")
for r in db.execute("SELECT id, title FROM hornet_nodes LIMIT 20").fetchall():
    print(f"  id={r[0]} {r[1][:80]}")

# Check the supplement file
print("\n--- Knowledge supplement file ---")
import os
supp_path = r'C:\Users\Administrator\AppData\Roaming\coworker\knowledge_supplements'
if os.path.exists(supp_path):
    for f in os.listdir(supp_path):
        print(f"  {f}")
else:
    print("  knowledge_supplements directory not found")

db.close()

# Also check knowledge.db
db2 = sqlite3.connect(r'C:\Users\Administrator\AppData\Roaming\coworker\knowledge.db')
tables2 = [r[0] for r in db2.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print(f"\nknowledge.db tables: {tables2}")
db2.close()
