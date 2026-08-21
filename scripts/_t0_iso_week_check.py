from datetime import date
d = date(2026, 8, 3)
iso = d.isocalendar()
print(f"ISO year-week: {iso[0]}-W{iso[1]:02d}")
print(f"Last week Monday: {d.isoformat()}  Sunday: {date(2026,8,9).isoformat()}")
