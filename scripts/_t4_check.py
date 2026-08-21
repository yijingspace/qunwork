# -*- coding: utf-8 -*-
import hashlib, os

base = r"E:\QunWork\QunWork\weekly_reports"
w31 = os.path.join(base, "2026-W31_周报.md")
w32 = os.path.join(base, "2026-W32_周报.md")

def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

print("W31 exists:", os.path.exists(w31))
print("W32 exists:", os.path.exists(w32))
if os.path.exists(w31):
    print("W31 sha256:", sha256(w31))
    print("W31 size:", os.path.getsize(w31))
if os.path.exists(w32):
    print("W32 sha256:", sha256(w32))
    print("W32 size:", os.path.getsize(w32))
print("dir listing:")
for name in sorted(os.listdir(base)):
    print(" -", name, os.path.getsize(os.path.join(base, name)))
