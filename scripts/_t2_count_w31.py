# -*- coding: utf-8 -*-
import io, glob, os
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
os.chdir(ROOT)
out = []
for f in sorted(glob.glob('*.md')):
    if '2026W31' in f:
        s = io.open(f, encoding='utf-8').read()
        han = sum(1 for c in s if '\u4e00' <= c <= '\u9fff')
        out.append('file=%s total=%d han=%d' % (f, len(s), han))
with io.open(os.path.join(HERE, '_t2_count_out.txt'), 'w', encoding='utf-8') as w:
    w.write('\n'.join(out) if out else 'NO MATCH')
