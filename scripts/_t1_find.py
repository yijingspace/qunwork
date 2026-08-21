# -*- coding: utf-8 -*-
"""Find db/json/log files under state-ish dirs."""
import os

hits = []
for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d not in (
        'node_modules', '.git', '.venv', 'venv', '__pycache__', 'dist', 'build', 'surfaces', 'target')]
    for f in files:
        if f.endswith(('.db', '.sqlite', '.json', '.log', '.ndjson', '.jsonl')):
            p = os.path.join(root, f)
            try:
                sz = os.path.getsize(p)
            except OSError:
                sz = -1
            hits.append((p, sz))
for p, sz in sorted(hits):
    print(sz, p)
