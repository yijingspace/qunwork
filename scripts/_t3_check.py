# -*- coding: utf-8 -*-
import re, io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
p = r'周报_2026W31_下周计划_章节草稿.md'
t = open(p, encoding='utf-8').read()
han = len(re.findall(r'[\u4e00-\u9fff]', t))
print('total_chars:', len(t))
print('han_chars:', han)
print('lines:', t.count('\n') + 1)
for key in ['## 🗓 三、下周计划', '### 3.1 下周目标', '### 3.2 具体任务', '### 3.3 风险与依赖',
            'P0-1', 'P0-2', 'P0-3', 'P1-1', 'P1-2', 'P1-3', 'P1-4', 'P1-5', 'P2-1', 'P2-2',
            '2026-08-10', '08-10', '08-14', 'executor 崩溃', 'GBK', '验收标准', '时间安排总览']:
    print(key, '->', key in t)
