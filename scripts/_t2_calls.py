# -*- coding: utf-8 -*-
"""T2: find knowledge-store call sites in coworker/ and top-level scripts."""
import os, re, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
pats = re.compile(r'add_text|index_file|scan_workspace|index_folder|KnowledgeStore|knowledge|sink|ingest|source_run_id', re.I)
roots = ['coworker', 'scripts']
skip = {'__pycache__', 'node_modules', '.git', 'dist', 'build'}
for root in roots:
    if not os.path.isdir(root):
        continue
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip]
        for f in files:
            if not f.endswith('.py'):
                continue
            p = os.path.join(dirpath, f)
            try:
                txt = open(p, encoding='utf-8', errors='ignore').read()
            except Exception:
                continue
            for i, ln in enumerate(txt.splitlines(), 1):
                if pats.search(ln):
                    s = ln.strip()
                    if len(s) > 160:
                        s = s[:160] + '...'
                    print(f'{p}:{i}: {s}')
