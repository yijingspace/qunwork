import sqlite3

hornet_db = r'C:\Users\Administrator\AppData\Roaming\coworker\hornet.db'
hconn = sqlite3.connect(hornet_db)

# Check all isolated nodes
hconn.row_factory = sqlite3.Row
isolated = hconn.execute("""
    SELECT n.id, n.title, n.kb_item_id, substr(n.content,1,120) as snippet
    FROM hornet_nodes n
    WHERE NOT EXISTS (SELECT 1 FROM hornet_edges e WHERE e.src=n.id OR e.dst=n.id)
    ORDER BY n.id DESC
""").fetchall()
print(f"Isolated nodes: {len(isolated)}")
for n in isolated:
    print(f"  id={n['id']} title='{n['title']}' kb={n['kb_item_id']}")

# Verify 9405 status
r = hconn.execute('SELECT id FROM hornet_nodes WHERE id=9405').fetchone()
print(f"\nNode 9405 in nodes table: {'YES' if r else 'NO'}")
r2 = hconn.execute('SELECT count(*) FROM hornet_edges WHERE src=9405 OR dst=9405').fetchone()[0]
print(f"Edges referencing 9405: {r2}")

# Total
tn = hconn.execute('SELECT count(*) FROM hornet_nodes').fetchone()[0]
te = hconn.execute('SELECT count(*) FROM hornet_edges').fetchone()[0]
print(f"\nTotals: {tn} nodes, {te} edges")

hconn.close()
