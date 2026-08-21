# -*- coding: utf-8 -*-
import datetime
for ts in [1786029922, 1786372176, 1786373186, 1786375895, 1786474087, 1786928400, 1786474184]:
    print(ts, "->", datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S'))
