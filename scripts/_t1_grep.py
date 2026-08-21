# -*- coding: utf-8 -*-
"""T1: grep working tree for weekly report / 周报 references."""
import os, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
pats = ['weekly', '周报', '本周', '成果清单', 'milestone', '5.1.', '5.2.']
skip = {'.git', 'node_modules', '__pycache__', '.pytest_cache', '.tmp-ws', 'target', 'dist'}
hits = {}
for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d not in skip]
    for f in files:
        if not f.endswith(('.py', '.md', '.ts', '.tsx', '.json', '.toml', '.yml', '.yaml', '.txt', '.db')):
            continue
        p = os.path.join(root, f)
        try:
            with open(p, 'rb') as fh:
                data = fh.read()
        except Exception:
            continue
        if b'\x00' in data[:2000]:
            continue
        try:
            txt = data.decode('utf-8', errors='ignore')
        except Exception:
            continue
        low = txt.lower()
        for pat in pats:
            if pat.lower() in low:
                hits.setdefault(p, set()).add(pat)
for p, ps in sorted(hits.items()):
    print(p, '->', sorted(ps))
