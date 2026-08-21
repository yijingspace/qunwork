"""W32 numstat per-commit sum (for 口径勘误 comparison vs net diff) + unique files count."""
import subprocess

SINCE, UNTIL = "2026-08-03 00:00", "2026-08-10 00:00"

out = subprocess.run(
    ["git", "log", "--since=" + SINCE, "--until=" + UNTIL, "--numstat", "--format=%h"],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
).stdout

ins = dels = files = 0
for line in out.splitlines():
    parts = line.split("\t")
    if len(parts) == 3 and parts[0] != "-" and parts[1] != "-":
        try:
            ins += int(parts[0]); dels += int(parts[1]); files += 1
        except ValueError:
            pass
print(f"numstat sum: files={files} ins=+{ins} dels=-{dels}")

names = subprocess.run(
    ["git", "log", "--since=" + SINCE, "--until=" + UNTIL, "--name-only", "--format="],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
).stdout
uniq = sorted({n for n in names.splitlines() if n.strip()})
print(f"unique files touched: {len(uniq)}")
