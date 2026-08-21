# -*- coding: utf-8 -*-
"""T3: preview all report md files."""
import glob, os, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
files = sorted(glob.glob('_swarm_reports/*.md')) + sorted(
    f for f in glob.glob('*.md') if not any(x in f for x in ('README', 'LICENSE', 'NOTICE'))
)
for f in files:
    try:
        t = open(f, encoding='utf-8').read()
    except Exception as e:
        print('ERR', f, e)
        continue
    lines = [l for l in t.splitlines() if l.strip()][:2]
    print(f, '|', os.path.getsize(f), 'bytes |', len(t), 'chars')
    for x in lines:
        print('   ', x[:100])
